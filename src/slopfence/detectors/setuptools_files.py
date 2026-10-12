"""Dependencies declared in setup.cfg and setup.py (SLOP001).

Both are read as text. ``setup.py`` is parsed with ``ast`` and never executed:
only values that are written out in the file are understood, and anything computed
is skipped silently.
"""

from __future__ import annotations

import ast
import configparser
import re
from collections.abc import Iterator

from slopfence.detectors.dependency import (
    MARKER_NAMES,
    REQ_COMMENT,
    Dependency,
    DependencyFile,
    Kind,
    Requirement,
    parse_requirement,
    requirements_comment,
    suppresses_slop001,
    text_lines,
)
from slopfence.indexes import is_public_index
from slopfence.source import IGNORE_FILE_RE, SourceFile, char_column, walk

# setup() keywords and [options] keys that hold requirement lists.
REQUIRES_KEYS = ("install_requires", "setup_requires", "tests_require")
EXTRAS_KEY = "extras_require"
# Links to pages or files where packages may come from instead of PyPI.
LINKS_KEY = "dependency_links"
# The setup.cfg section with the index and the links that setuptools itself installs
# from: the packages of ``setup_requires`` and ``tests_require``.
EASY_INSTALL = "easy_install"
_SECTION = re.compile(r"\[(?P<header>.+)\]")
# The option's name runs up to the first "=" or ":". White space at its end is cut
# afterwards: a pattern that skipped it would walk a long run of spaces once per space.
_OPTION = re.compile(r"(?P<option>[^=:]*)(?:[=:]\s*(?P<value>.*))?$")

# --- setup.cfg ----------------------------------------------------------------------

# One value line of an INI file: (line number, text, whether it is on the key's line).
_ValueLine = tuple[int, str, bool]


def _cfg_lines(text: str) -> Iterator[tuple[str, str, _ValueLine]]:
    """Every value line of an INI file, as (section, option, value line).

    Follows ``configparser``: a line indented deeper than its option continues the
    value, and lines starting with ``#`` or ``;`` are comments. Lines end at a line
    feed and nowhere else, as they do for git and for ``configparser`` (see
    ``dependency.text_lines``).
    """
    # A file with a line before its first section header never gets here:
    # ``configparser`` turns it down first (see ``_cfg_values``).
    section = ""
    option: str | None = None
    key_indent = 0
    for lineno, raw in enumerate(text_lines(text), start=1):
        stripped = raw.strip()
        if not stripped or stripped[0] in "#;":
            continue
        indent = len(raw) - len(raw.lstrip())
        if option is not None and indent > key_indent:
            yield section, option, (lineno, stripped, False)
            continue
        key_indent = indent
        if m := _SECTION.match(stripped):
            section, option = m.group("header"), None
            continue
        # The pattern matches any text, so there is always a match.
        option, value = _OPTION.match(stripped).group("option", "value")
        option = option.rstrip()
        if value and value.strip():
            yield section, option, (lineno, value.strip(), True)


def _cfg_values(text: str) -> dict[tuple[str, str], list[_ValueLine]] | None:
    """The value lines of every option of an INI file, by (section, option).

    None when the file is not one ``configparser`` reads (no section header, a section
    or key twice). Also None if ``configparser`` reads other value lines than were
    found here: the two follow the same rules, and a line number is only passed on
    while they agree.
    """
    parser = configparser.RawConfigParser()
    # Keep the case of option names: it is compared with the text of the file.
    parser.optionxform = str
    values: dict[tuple[str, str], list[_ValueLine]] = {}
    try:
        parser.read_string(text)
        for section, option, line in _cfg_lines(text):
            values.setdefault((section, option), []).append(line)
        agree = all(
            [v.strip() for v in parser.get(section, option).split("\n") if v.strip()]
            == [value for _, value, _ in lines]
            for (section, option), lines in values.items()
        )
    except (configparser.Error, ValueError):
        return None
    return values if agree else None


def _cfg_key(option: str) -> str:
    """An option name as setuptools compares it: ``Install-Requires`` is ``install_requires``."""
    return option.lower().replace("-", "_")


def _cfg_comments(text: str) -> list[str]:
    """The comment on each line ("" if none): full-line comments and trailing `` #``."""
    comments = []
    for raw in text_lines(text):
        stripped = raw.strip()
        comments.append(stripped if stripped.startswith("#") else requirements_comment(raw))
    return comments


def _cfg_requirements(lines: list[_ValueLine]) -> Iterator[tuple[int, Requirement]]:
    """The (line number, requirement) of each requirement in one option's value.

    One requirement per line. A value that is all on the key's line is cut at ``;``,
    as setuptools does: ``setup_requires = pbr; setuptools`` is two requirements. So
    is ``pytest; python_version < 3``, where the second part was meant as a marker:
    a part after the first that starts with a marker variable is not a package.
    """
    if lines[0][1].startswith("file:"):
        return  # the list lives in another file, which is checked on its own
    one_line = len(lines) == 1 and lines[0][2]
    for lineno, value, _ in lines:
        spec = REQ_COMMENT.sub("", value)
        if "%(" in spec:
            continue  # configparser interpolation: the real value isn't known
        for n, chunk in enumerate(spec.split(";") if one_line else [spec]):
            parsed = parse_requirement(chunk)
            if parsed and not (n and parsed.name in MARKER_NAMES):
                yield lineno, parsed


def _cfg_names_a_source(values: dict[tuple[str, str], list[_ValueLine]]) -> bool:
    """Whether a setup.cfg says that packages may come from another place than PyPI.

    That is ``[options] dependency_links``, ``[easy_install] find_links``, and
    ``[easy_install] index_url`` with an index that is not PyPI. An option without a
    value is not in ``values``, so it names nothing.
    """
    for (section, option), lines in values.items():
        key = _cfg_key(option)
        if (section, key) in {("options", LINKS_KEY), (EASY_INSTALL, "find_links")}:
            return True
        if (section, key) == (EASY_INSTALL, "index_url") and not is_public_index(lines[0][1]):
            return True
    return False


def parse_setup_cfg(text: str, rel: str) -> DependencyFile:
    """Dependencies in setup.cfg: ``[options]`` ``install_requires``, ``setup_requires``
    and ``tests_require``, and every key of ``[options.extras_require]``.

    Each is reported on its own line. A file that ``configparser`` can't read yields
    nothing.
    """
    found = DependencyFile(rel, Kind.SETUP_CFG)
    values = _cfg_values(text)
    if values is None:
        return found
    comments = _cfg_comments(text)
    ignore_file = any(IGNORE_FILE_RE.search(c) for c in comments)
    private = found.private_index = _cfg_names_a_source(values)
    for (section, option), lines in values.items():
        if section == "metadata" and _cfg_key(option) == "name":
            found.project_names.add(lines[0][1])
        if not (
            (section == "options" and _cfg_key(option) in REQUIRES_KEYS)
            or section == "options.extras_require"
        ):
            continue
        for lineno, parsed in _cfg_requirements(lines):
            found.dependencies.append(
                Dependency(
                    parsed.name,
                    rel,
                    lineno,
                    validate=not (parsed.direct or private),
                    suppressed=ignore_file or suppresses_slop001(comments[lineno - 1]),
                    own_source=parsed.direct,
                )
            )
    found.dependencies.sort(key=lambda dep: dep.line)
    return found


# --- setup.py -----------------------------------------------------------------------

# How deep names and nested expressions are followed. Real files need two or three
# steps. Without a limit, a generated file with a thousand nested expressions would
# end the run with a RecursionError.
_MAX_DEPTH = 20
# How many steps one file gets for following names and expressions. One step is taken
# per list and per string, so a file with 5,000 requirements takes about 5,000. A name
# is followed each time it is used: a list built from eight uses of the list before
# it, seven lists deep, would take more than two million (8 ** 7).
_MAX_STEPS = 50_000
_NO_NAMES: frozenset[str] = frozenset()


def _string(node: ast.AST) -> ast.Constant | None:
    """``node`` if it is a string literal, else None."""
    return node if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_empty(node: ast.expr) -> bool:
    """Whether ``node`` is written out as nothing: ``[]``, ``()``, ``""`` or ``None``."""
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return not node.elts
    return isinstance(node, ast.Constant) and not node.value


def _summands(node: ast.expr) -> list[ast.expr]:
    """The operands of ``a + b + c``, in order, however long the sum is."""
    right: list[ast.expr] = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        right.append(node.right)
        node = node.left
    return [node, *reversed(right)]


def _is_dict_call(node: ast.expr) -> bool:
    """Whether ``node`` is a ``dict(key=value, ...)`` call."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict"


def _assignment(node: ast.AST) -> tuple[list[ast.expr], ast.expr] | None:
    """The targets and the value of ``a = b = value`` or ``a: T = value``, else None."""
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return [node.target], node.value
    return None


def _assigned_name(target: ast.expr) -> str | None:
    """The name a target assigns to: ``name`` itself, or the ``name`` of ``name[key]``."""
    if isinstance(target, ast.Subscript):
        target = target.value
    return target.id if isinstance(target, ast.Name) else None


def _replaced_at(tree: ast.Module) -> dict[str, int]:
    """For each name, the line of the last top-level statement that replaces its value.

    ``extras = {...}`` at the top level of the file, where the new value doesn't use
    the old one, replaces whatever the name held. What was assigned above that line
    was meant for something else (``package_data``, say) and never reaches ``setup()``.
    An assignment inside ``if``, ``try`` or a function replaces nothing: it may not run.
    """
    lines: dict[str, int] = {}
    for statement in tree.body:
        if assignment := _assignment(statement):
            targets, value = assignment
            used = {node.id for node in walk(value) if isinstance(node, ast.Name)}
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in used:
                    lines[target.id] = statement.lineno
    return lines


class _SetupScript:
    """Literal requirement strings passed to ``setup()`` in a setup.py.

    Understands literals, ``+`` between lists, ``*unpacking``, conditional
    expressions, and names assigned in the file. Everything computed (calls,
    comprehensions, f-strings, string concatenation) is skipped.
    """

    def __init__(self, tree: ast.Module, nodes: list[ast.AST]) -> None:
        """Index the assignments of a file by the name they assign to.

        ``nodes`` is every node of ``tree`` (``SourceFile.nodes``). Assignments that a
        later one replaces are left out (see ``_replaced_at``).
        """
        self.tree = tree
        self.nodes = nodes
        # How many expressions were followed so far (see ``_MAX_STEPS``).
        self.steps = 0
        # name -> values from ``name = v`` and ``name: T = v``.
        self.assigned: dict[str, list[ast.expr]] = {}
        # name -> values from ``name += v``: more items for a list.
        self.added: dict[str, list[ast.expr]] = {}
        # name -> (key, value) from ``name[key] = v``.
        self.items: dict[str, list[tuple[ast.expr, ast.expr]]] = {}
        replaced = _replaced_at(tree)
        for node in nodes:
            if isinstance(node, ast.AugAssign):
                # ``name += v`` adds to a list. Any other operator builds something else.
                targets, value = ([node.target] if isinstance(node.op, ast.Add) else []), node.value
            elif assignment := _assignment(node):
                targets, value = assignment
            else:
                continue
            for target in targets:
                name = _assigned_name(target)
                if name is None or node.lineno < replaced.get(name, 0):
                    continue
                if isinstance(target, ast.Subscript):
                    self.items.setdefault(name, []).append((target.slice, value))
                elif isinstance(node, ast.AugAssign):
                    self.added.setdefault(name, []).append(value)
                else:
                    self.assigned.setdefault(name, []).append(value)

    def _too_far(self, depth: int) -> bool:
        """Count one step, and say whether to stop: the expression is nested too
        deeply, or the file has used up its steps."""
        self.steps += 1
        return depth > _MAX_DEPTH or self.gave_up

    @property
    def gave_up(self) -> bool:
        """Whether the file needed more steps than it gets. What was read is then
        only a part, cut off at no particular place."""
        return self.steps > _MAX_STEPS

    def strings(
        self, node: ast.expr, seen: frozenset[str] = _NO_NAMES, depth: int = 0
    ) -> list[ast.Constant]:
        """The string literals a single-string expression can be: a literal, a
        conditional expression, or a name that only ever holds a string.

        A name that is also assigned something else, or added to, gives nothing. In
        ``reqs = "requirements.txt"`` followed by ``reqs = open(reqs).readlines()``,
        the string is a file name, not a requirement.
        """
        if self._too_far(depth):
            return []
        if literal := _string(node):
            return [literal]
        if isinstance(node, ast.IfExp):
            return self.strings(node.body, seen, depth + 1) + self.strings(
                node.orelse, seen, depth + 1
            )
        if isinstance(node, ast.Name) and node.id not in seen and node.id not in self.added:
            values = [
                self.strings(value, seen | {node.id}, depth + 1)
                for value in self.assigned.get(node.id, ())
            ]
            return [literal for value in values for literal in value] if all(values) else []
        return []

    def _list(self, node: ast.expr, seen: frozenset[str], depth: int) -> list[ast.Constant] | None:
        """The string literals in a list-like expression, or None if ``node`` isn't
        certainly a list (so ``"pkg==" + version`` never counts as two requirements)."""
        if self._too_far(depth):
            return None
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            found: list[ast.Constant] = []
            for element in node.elts:
                if isinstance(element, ast.Starred):
                    found += self._list(element.value, seen, depth + 1) or []
                else:
                    found += self.strings(element, seen, depth + 1)
            return found
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            parts = _summands(node)
        elif isinstance(node, ast.IfExp):
            parts = [node.body, node.orelse]
        elif isinstance(node, ast.Name) and node.id not in seen:
            parts = self.assigned.get(node.id, []) + self.added.get(node.id, [])
            seen = seen | {node.id}
        else:
            return None
        lists = [
            found for part in parts if (found := self._list(part, seen, depth + 1)) is not None
        ]
        return [literal for found in lists for literal in found] if lists else None

    def requirements(
        self, node: ast.expr, seen: frozenset[str] = _NO_NAMES, depth: int = 0
    ) -> list[ast.Constant]:
        """String literals of a requirement list, or of a single requirement string."""
        found = self._list(node, seen, depth)
        return self.strings(node, seen, depth) if found is None else found

    def extras(
        self, node: ast.expr, seen: frozenset[str] = _NO_NAMES, depth: int = 0
    ) -> list[ast.Constant]:
        """String literals in the values of an ``extras_require`` mapping."""
        if self._too_far(depth):
            return []
        found: list[ast.Constant] = []
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:  # {**other}
                    found += self.extras(value, seen, depth + 1)
                else:
                    found += self.requirements(value, seen, depth + 1)
        elif _is_dict_call(node):
            for keyword in node.keywords:
                if keyword.arg is not None:
                    found += self.requirements(keyword.value, seen, depth + 1)
        elif isinstance(node, ast.Name) and node.id not in seen:
            for value in self.assigned.get(node.id, ()):
                found += self.extras(value, seen | {node.id}, depth + 1)
            for _, value in self.items.get(node.id, ()):
                found += self.requirements(value, seen | {node.id}, depth + 1)
        return found

    def keyword_values(self, call: ast.Call, name: str) -> list[ast.expr]:
        """The values passed to ``setup()`` for one keyword: ``name=...``, or through
        ``**kwargs`` when ``kwargs`` is a dict literal, ``dict(...)`` or ``kwargs[name] = ...``."""
        found: list[ast.expr] = []
        for keyword in call.keywords:
            if keyword.arg == name:
                found.append(keyword.value)
            elif keyword.arg is None:
                found += self._mapping_values(keyword.value, name, _NO_NAMES, 0)
        return found

    def _mapping_values(
        self, node: ast.expr, name: str, seen: frozenset[str], depth: int
    ) -> list[ast.expr]:
        """Values stored under the key ``name`` in a mapping expression."""
        if self._too_far(depth):
            return []
        found: list[ast.expr] = []
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    found += self._mapping_values(value, name, seen, depth + 1)
                elif isinstance(key, ast.Constant) and key.value == name:
                    found.append(value)
        elif _is_dict_call(node):
            found += [keyword.value for keyword in node.keywords if keyword.arg == name]
        elif isinstance(node, ast.Name) and node.id not in seen:
            for key, value in self.items.get(node.id, ()):
                if isinstance(key, ast.Constant) and key.value == name:
                    found.append(value)
            for value in self.assigned.get(node.id, ()):
                found += self._mapping_values(value, name, seen | {node.id}, depth + 1)
        return found

    def setup_calls(self) -> Iterator[ast.Call]:
        """Calls to ``setup(...)`` or ``<module>.setup(...)``.

        A file that defines a ``setup`` of its own (a task runner, a Sphinx
        extension) calls that one, so its plain ``setup(...)`` calls don't count.
        """
        own = any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == "setup"
            for node in self.tree.body
        )
        for node in self.nodes:
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                if node.func.id == "setup" and not own:
                    yield node
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "setup":
                yield node


def _string_lines(literal: ast.Constant, lines: list[str]) -> Iterator[tuple[str, int, int]]:
    """Each line of a string literal with its (line, column) in the file.

    A literal can hold several requirements, one per line. Lines after the first
    get their own line number only when the literal spans as many physical lines
    as it has line breaks (a plain triple-quoted string).
    """
    parts = literal.value.split("\n")
    spans = (literal.end_lineno or literal.lineno) - literal.lineno == len(parts) - 1
    first = lines[literal.lineno - 1] if literal.lineno <= len(lines) else ""
    col = char_column(first, literal.col_offset) + 1
    for n, part in enumerate(parts):
        if n and spans:
            index = literal.lineno - 1 + n
            physical = lines[index] if index < len(lines) else ""
            yield part, literal.lineno + n, len(physical) - len(physical.lstrip()) + 1
        else:
            yield part, literal.lineno, col


def parse_setup_py(src: SourceFile) -> DependencyFile:
    """Dependencies passed to ``setup()`` in a setup.py, when they are written out.

    Read from the syntax tree: the file is never run. Each dependency is reported at
    the string literal that names it. A file whose expressions take more steps to
    follow than a file gets (``_MAX_STEPS``) declares no dependency.
    """
    found = DependencyFile(src.rel, Kind.SETUP_PY)
    script = _SetupScript(src.tree, src.nodes)
    lines = text_lines(src.text)
    comments = {comment.line: comment.text for comment in src.comments}
    seen: set[tuple[int, int, str]] = set()
    for call in script.setup_calls():
        for value in script.keyword_values(call, "name"):
            found.project_names.update(literal.value for literal in script.strings(value))
        # dependency_links (even computed ones) mean packages may come from elsewhere.
        # An empty list, as a project template leaves it, holds no link.
        private = not all(_is_empty(value) for value in script.keyword_values(call, LINKS_KEY))
        found.private_index = found.private_index or private
        literals: list[ast.Constant] = []
        for keyword in REQUIRES_KEYS:
            for value in script.keyword_values(call, keyword):
                literals += script.requirements(value)
        for value in script.keyword_values(call, EXTRAS_KEY):
            literals += script.extras(value)
        for literal in literals:
            for text, line, col in _string_lines(literal, lines):
                parsed = parse_requirement(REQ_COMMENT.sub("", text))
                if parsed is None or (line, col, parsed.name) in seen:
                    continue
                seen.add((line, col, parsed.name))
                found.dependencies.append(
                    Dependency(
                        parsed.name,
                        src.rel,
                        line,
                        validate=not (parsed.direct or private),
                        suppressed=src.ignore_file or suppresses_slop001(comments.get(line, "")),
                        col=col,
                        own_source=parsed.direct,
                    )
                )
    if script.gave_up:
        found.dependencies = []
    found.dependencies.sort(key=lambda dep: (dep.line, dep.col, dep.name))
    return found
