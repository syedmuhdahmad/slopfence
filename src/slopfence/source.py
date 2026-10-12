"""Parsed view of a Python file: AST, comments, docstrings and ignore directives."""

from __future__ import annotations

import ast
import io
import re
import tokenize
import warnings
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

IGNORE_RE = re.compile(r"#\s*slopfence:\s*ignore(?:\[(?P<rules>[A-Z0-9,\s]+)\])?(?!-)", re.I)
_ESCAPE_SEQ = re.compile(r"\\[ntr]")
EXAMPLE_DIRS = {"demo", "demos", "example", "examples", "sample", "samples"}
IGNORE_FILE_RE = re.compile(r"#\s*slopfence:\s*ignore-file\b", re.I)
# The only nodes that can hold statements. Expressions never do, and they are most
# of a tree, so a search for functions, classes, ``if`` or ``try`` can skip them.
_STATEMENT_HOLDERS = (ast.stmt, ast.excepthandler, ast.match_case)


def children(node: ast.AST, kinds: type | tuple[type, ...] = ast.AST) -> list[ast.AST]:
    """The direct children of ``node`` that are ``kinds``, in ``ast.iter_child_nodes`` order."""
    found = []
    for name in node._fields:
        value = getattr(node, name, None)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, kinds):
                    found.append(item)
        elif isinstance(value, kinds):
            found.append(value)
    return found


def walk(node: ast.AST) -> list[ast.AST]:
    """``node`` and everything below it, as a list in ``ast.walk`` order.

    Faster than ``ast.walk``, which goes through two generators per node.
    """
    nodes = [node]
    # The list grows while it is read, which makes this breadth-first like ``ast.walk``.
    for current in nodes:
        nodes.extend(children(current))
    return nodes


def child_statements(node: ast.AST) -> list[ast.AST]:
    """The children of ``node`` that can hold statements, in source order.

    These are statements, ``except`` handlers and ``match`` cases. Following only
    them reaches every statement of a tree without visiting its expressions.
    """
    return children(node, _STATEMENT_HOLDERS)


@dataclass
class TextSpan:
    """A line of comment or docstring text with its location (1-based line and column)."""

    line: int
    col: int
    text: str


@dataclass
class SourceFile:
    """One parsed Python file: its text, syntax tree, comments and ignore directives."""

    path: Path
    rel: str
    text: str
    tree: ast.Module
    comments: list[TextSpan] = field(default_factory=list)
    # line -> rule IDs to ignore on that line; None means every rule.
    ignores: dict[int, set[str] | None] = field(default_factory=dict)
    ignore_file: bool = False

    @property
    def is_test_file(self) -> bool:
        """Whether this is test code (see ``is_test_path``)."""
        return is_test_path(self.path.name, self.rel)

    @property
    def is_example_file(self) -> bool:
        """Files under demo/ or examples/ folders, or named demo.py, demo_x.py, x_example.py."""
        folders = {p.lower() for p in Path(self.rel).parts[:-1]}
        words = set(self.path.stem.lower().split("_"))
        return bool(folders & EXAMPLE_DIRS or words & EXAMPLE_DIRS)

    @cached_property
    def nodes(self) -> list[ast.AST]:
        """Every node of the tree in ``ast.walk`` order, walked once and shared by all rules."""
        return walk(self.tree)

    @cached_property
    def docstring_lines(self) -> list[TextSpan]:
        """Each line of every module, class and function docstring."""
        return list(self._iter_docstring_lines())

    def _iter_docstring_lines(self) -> Iterator[TextSpan]:
        """Yield the non-empty lines of every docstring, with their positions."""
        # Use the physical source lines, not the decoded string value: escapes like
        # "\n" and backslash-continuations would otherwise shift line numbers.
        lines = self.text.split("\n")
        nodes = [self.tree] + [
            n
            for n in self.nodes
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in nodes:
            body = node.body
            if not (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                continue
            const = body[0].value
            end = const.end_lineno or const.lineno
            for lineno in range(const.lineno, min(end, len(lines)) + 1):
                text = lines[lineno - 1]
                if not text.strip():
                    continue
                if lineno == const.lineno:
                    # ast columns are UTF-8 byte offsets; convert to characters.
                    col = len(text.encode()[: const.col_offset].decode("utf-8", "replace"))
                else:
                    col = len(text) - len(text.lstrip())
                # Treat escapes like \n as word breaks so "Intro\nI hope this helps" matches.
                yield TextSpan(lineno, col + 1, _ESCAPE_SEQ.sub(" ", text))

    def text_spans(self) -> Iterator[TextSpan]:
        """Comments and docstring lines: where AI chat text and placeholders hide."""
        yield from self.comments
        yield from self.docstring_lines


def is_test_path(name: str, rel: str) -> bool:
    """Whether a file is test code, from its name and its project-relative path."""
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        # Relative parts, so a project that lives under ~/tests/ isn't all "test code".
        or bool({"tests", "test"} & set(Path(rel).parts[:-1]))
    )


def relative_path(path: Path, root: Path) -> str:
    """``path`` relative to ``root`` as a POSIX string, or as given if it's outside."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def read_source(path: Path) -> str | None:
    """Read a file honouring PEP 263 coding cookies and a UTF-8 BOM, like Python does."""
    try:
        with tokenize.open(path) as f:
            return f.read()
    except (OSError, SyntaxError, UnicodeDecodeError, LookupError):
        return None


def load(path: Path, root: Path, comments: bool = True) -> SourceFile | None:
    """Parse a file. Returns None if it is not valid Python.

    With ``comments`` False the comments are not read, so the file has none and no
    ignore directives. That is enough for a file that is only compared with (SLOP030).
    """
    text = read_source(path)
    if text is None:
        return None
    try:
        # Python warns about some valid code (an invalid escape sequence, for one).
        # What happens then depends on the warning filters of the process: under
        # ``-W error`` the file would not parse. A worker process doesn't always have
        # the filters of the main one, so the answer must not depend on them.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text, filename=str(path))
    except (SyntaxError, ValueError):
        return None

    src = SourceFile(path=path, rel=relative_path(path, root), text=text, tree=tree)
    if not comments or "#" not in text:
        return src  # nothing to tokenize for
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type != tokenize.COMMENT:
                continue
            line, col = tok.start
            src.comments.append(TextSpan(line, col + 1, tok.string))
            if IGNORE_FILE_RE.search(tok.string):
                src.ignore_file = True
            elif m := IGNORE_RE.search(tok.string):
                rules = m.group("rules")
                src.ignores[line] = (
                    {r.strip().upper() for r in rules.split(",") if r.strip()} if rules else None
                )
    except (tokenize.TokenError, IndentationError):
        pass
    return src


def top_level_imports(src: SourceFile) -> list[tuple[str, int, int]]:
    """(top-level module, line, column) of every absolute import, in ``ast.walk`` order."""
    found = []
    for node in src.nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name.split(".")[0], node.lineno, node.col_offset + 1))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.module.split(".")[0], node.lineno, node.col_offset + 1))
    return found
