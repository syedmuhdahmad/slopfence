"""Jupyter notebooks as a source of dependencies and imports (SLOP001).

A notebook is JSON. Only code cells are read, and only for two things:

* ``pip install`` lines (``%pip install x``, ``!pip install x``, ...): the
  packages are declared dependencies;
* imports, which are only looked up with ``--check-imports``.

Every result carries the line and column in the ``.ipynb`` file itself, so
``--diff`` and SARIF work. The position of each source string is taken from the
JSON text while it is decoded, so it is exact for any formatting: Jupyter's
one-string-per-line layout, other indentation, or a minified file.
"""

from __future__ import annotations

import ast
import json
import re
from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from slopfence.detectors.dependency import (
    Dependency,
    DependencyFile,
    Kind,
    requirements_comment,
)
from slopfence.detectors.pip_commands import (
    PipInstall,
    Word,
    pip_install_arguments,
    pip_requirements,
    shell_commands,
    unread_pip_install,
)
from slopfence.source import (
    ALL_RULES,
    IGNORE_FILE_RE,
    SourceFile,
    char_column,
    line_ignores,
    parse,
    top_level_imports,
)

# A notebook larger than this is not read. The whole file has to be in memory to be
# decoded, with its images and outputs. Notebooks that people write stay far below it.
MAX_BYTES = 20 * 1024 * 1024
# A shell line longer than this is generated text, not something a person typed.
MAX_COMMAND_LENGTH = 4000

# --- Reading the JSON with positions -----------------------------------------------

_WS = re.compile(r"[ \t\n\r]*")
_DECODER = json.JSONDecoder()
# One escape in a JSON string body. A surrogate pair decodes to a single character.
_ESCAPE = re.compile(
    r"\\(?:u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)", re.S
)


class _Reader:
    """Walks JSON text and remembers where it is. Values are decoded by ``json``."""

    def __init__(self, text: str) -> None:
        """Start at the beginning of ``text``."""
        self.text = text
        self.i = 0

    def peek(self) -> str:
        """The next non-blank character ("" at the end)."""
        self.i = _WS.match(self.text, self.i).end()
        return self.text[self.i : self.i + 1]

    def _take(self, char: str) -> bool:
        """Move past ``char`` if it comes next."""
        if self.peek() == char:
            self.i += 1
            return True
        return False

    def _expect(self, char: str) -> None:
        """Move past ``char``, which must come next."""
        if not self._take(char):
            raise ValueError(f"expected {char!r} at offset {self.i}")

    def value(self) -> object:
        """Decode the value that starts here and move past it."""
        self.peek()
        obj, self.i = _DECODER.raw_decode(self.text, self.i)
        return obj

    def object_keys(self) -> Iterator[str]:
        """The keys of the object that starts here. The caller reads each value."""
        self._expect("{")
        if self._take("}"):
            return
        while True:
            key = self.value()
            if not isinstance(key, str):
                raise ValueError("object key is not a string")
            self._expect(":")
            yield key
            if self._take("}"):
                return
            self._expect(",")

    def array_items(self) -> Iterator[None]:
        """One step per element of the array that starts here. The caller reads each."""
        self._expect("[")
        if self._take("]"):
            return
        while True:
            yield None
            if self._take("]"):
                return
            self._expect(",")


@dataclass
class Cell:
    """A code cell: its text, and where each piece of it sits in the file."""

    text: str = ""
    # Offset in ``text`` where each source string starts.
    starts: list[int] = field(default_factory=list)
    # (start, end) of each source string's JSON token in the file, quotes included.
    spans: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class Notebook:
    """The code cells of a notebook, and the text of the file they were read from."""

    text: str
    cells: list[Cell]
    # Offset of the start of every line of ``text``. Worked out when first asked for.
    _line_starts: list[int] = field(default_factory=list)

    def position(self, cell: Cell, offset: int) -> tuple[int, int]:
        """The 1-based (line, column) in the file of character ``offset`` of a cell."""
        if not self._line_starts:
            self._line_starts = [0] + [m.end() for m in re.finditer("\n", self.text)]
        piece = max(bisect_right(cell.starts, offset) - 1, 0)
        start, end = cell.spans[piece]
        body = self.text[start + 1 : end - 1]
        at = start + 1 + _raw_length(body, offset - cell.starts[piece])
        line = bisect_right(self._line_starts, at)
        return line, at - self._line_starts[line - 1] + 1


def _raw_length(body: str, count: int) -> int:
    """How many characters of a JSON string body encode its first ``count`` characters."""
    if "\\" not in body:
        return count
    pos = decoded = 0
    for m in _ESCAPE.finditer(body):
        plain = m.start() - pos
        if decoded + plain >= count:
            break
        decoded += plain + 1
        pos = m.end()
    return pos + count - decoded


def _read_source(reader: _Reader) -> Cell | None:
    """A cell's ``source``: one string, or a list of strings. None for anything else."""
    cell = Cell()
    parts: list[str] = []
    length = 0

    def add() -> bool:
        """Read one string and remember where it was."""
        nonlocal length
        reader.peek()
        start = reader.i
        value = reader.value()
        if not isinstance(value, str):
            return False
        cell.starts.append(length)
        cell.spans.append((start, reader.i))
        parts.append(value)
        length += len(value)
        return True

    ok = True
    if reader.peek() == "[":
        for _ in reader.array_items():
            ok = add() and ok
    else:
        ok = add()
    cell.text = "".join(parts)
    return cell if ok and parts else None


def _read_cells(reader: _Reader) -> list[Cell]:
    """The code cells of the ``cells`` array that starts here."""
    cells: list[Cell] = []
    for _ in reader.array_items():
        if reader.peek() != "{":
            reader.value()
            continue
        cell_type: object = None
        source: Cell | None = None
        for key in reader.object_keys():
            if key == "source":
                source = _read_source(reader)
            elif key == "cell_type":
                cell_type = reader.value()
            else:
                reader.value()
        if cell_type == "code" and source is not None:
            cells.append(source)
    return cells


def _is_python(info: dict[str, object]) -> bool:
    """Whether the top-level values of a notebook say it is a Python notebook this
    module can read: format 4, and no kernel language other than Python."""
    if info.get("nbformat") != 4:
        return False
    metadata = info.get("metadata")
    for section, name in (("kernelspec", "language"), ("language_info", "name")):
        entry = metadata.get(section) if isinstance(metadata, dict) else None
        language = entry.get(name) if isinstance(entry, dict) else None
        if isinstance(language, str) and language.lower() != "python":
            return False
    return True


def read_notebook(text: str) -> Notebook | None:
    """The code cells of a Python notebook (nbformat 4), or None if it isn't one.

    Never raises: invalid JSON, another notebook format or another kernel
    language all give None. Of a key that is there twice, the last one counts, as
    it does for every JSON reader.
    """
    reader = _Reader(text)
    cells: list[Cell] = []
    info: dict[str, object] = {}
    try:
        for key in reader.object_keys():
            if key == "cells" and reader.peek() == "[":
                cells = _read_cells(reader)
            else:
                info[key] = reader.value()
        if reader.peek():
            return None  # text after the document
    except (ValueError, RecursionError):
        return None
    return Notebook(text, cells) if _is_python(info) else None


# --- Cells as Python ---------------------------------------------------------------

_SHELL_CELL = re.compile(r"\s*%%(?:bash|sh|shell)\b")
_AUTOMAGIC = re.compile(r"pip\s+install\b")
# A ``cd`` line without the ``%``. IPython runs it as the magic all the same, unless
# what follows starts like an assignment or a tuple (``cd = 3``, ``cd, x = 1, 2``).
_AUTOMAGIC_CD = re.compile(r"^[ \t]*cd[ \t]+[^=,\s]", re.M)
# Cell magics whose body is still Python.
_PYTHON_CELL_MAGICS = {"time", "timeit", "capture", "prun", "debug", "python", "python3"}
_CELL_MAGIC = re.compile(r"\s*%%(\w*)")
# Cell magics that save the cell's body as a file.
_FILE_CELL_MAGICS = {"writefile", "file"}
_ASSIGNED_COMMAND = re.compile(r"\s*[A-Za-z_][\w.,\s\[\]]*=\s*[!%]")
# A carriage return that no line feed follows. Python's parser ends a line there, and
# nothing else that reads a cell does.
_LONE_CARRIAGE_RETURN = re.compile(r"\r(?!\n)")
# Expressions that hold string text over several lines (t-strings since Python 3.14).
_STRINGS = tuple(
    kind
    for kind in (ast.Constant, ast.JoinedStr, getattr(ast, "TemplateStr", None))
    if kind is not None
)


@dataclass
class CellView:
    """What one cell contains, by offset into the cell's text."""

    # (command start offset, 0-based line index) of each IPython or shell line.
    commands: list[tuple[int, int]] = field(default_factory=list)
    # Imports as (top-level module, line number in the cell, 0-based character column).
    imports: list[tuple[str, int, int]] = field(default_factory=list)
    # The rules that an ignore comment switches off, by line of the cell (1-based).
    ignores: dict[int, frozenset[str]] = field(default_factory=dict)
    # The modules the cell writes: ``%%writefile pkg/mod.py`` gives ``pkg`` and ``mod``.
    written: set[str] = field(default_factory=set)
    # A line of the cell is ``cd <folder>`` without the ``%``: the same magic.
    changes_folder: bool = False


def _is_magic_line(stripped: str) -> bool:
    """Whether a line is IPython syntax rather than Python."""
    return (
        stripped.startswith(("%", "!", "?"))
        or stripped.endswith("?")
        or bool(_AUTOMAGIC.match(stripped))
        or bool(_ASSIGNED_COMMAND.match(stripped))
    )


def _parse_cell(text: str) -> SourceFile | None:
    """A cell parsed as Python, or None if it is not Python (see ``source.parse``)."""
    return parse(text, Path("<cell>"), "<cell>")


def _without_magics(lines: list[str], skip_first: bool) -> tuple[list[str], list[tuple[int, int]]]:
    """A cell's lines with every IPython line replaced by ``pass``, and the command
    lines among them as (command start offset, 0-based line index).

    A command line starts with ``!`` or ``%``, or is a bare ``pip install``. The
    replaced lines keep their indentation, so the result parses where the cell was
    Python around its magics. ``skip_first`` blanks the first line: a cell magic.

    A carriage return at the end of a line is no indentation. Kept in front of
    ``pass``, it would be one more line for Python's parser, and every import below
    it would be found one line too low.
    """
    python = list(lines)
    commands: list[tuple[int, int]] = []
    continued = False
    offset = 0
    for n, line in enumerate(lines):
        stripped = line.strip()
        body = line.removesuffix("\r")
        indent = len(body) - len(body.lstrip())
        if n == 0 and skip_first:
            python[n] = ""
        elif continued or _is_magic_line(stripped):
            if not continued and (stripped.startswith(("!", "%")) or _AUTOMAGIC.match(stripped)):
                marker = len(stripped) - len(stripped.lstrip("!%"))
                commands.append((offset + indent + marker, n))
            python[n] = line[:indent] + "pass"
            # A line that ends with a backslash goes on in the next one.
            continued = stripped.endswith("\\")
        else:
            continued = False
        offset += len(line) + 1
    return python, commands


def view_cell(text: str, want_imports: bool) -> CellView:
    """Find the command lines of a cell and, if it parses as Python, its imports
    and the ignore comments on its lines. A cell that doesn't parse is not an error.

    No import is taken from a cell with a carriage return inside a line. The parser
    counts one more line there than the cell has, so an import could not be matched
    to its line: to its place in the file, or to the ignore comment on it.
    """
    view = CellView()
    lines = text.split("\n")
    magic = _CELL_MAGIC.match(lines[0])
    if magic and _SHELL_CELL.match(lines[0]):
        # Every line of a %%bash cell is a shell command.
        offset = 0
        for n, line in enumerate(lines):
            if n and line.strip():
                view.commands.append((offset + len(line) - len(line.lstrip()), n))
            offset += len(line) + 1
        return view
    if magic and magic.group(1) not in _PYTHON_CELL_MAGICS:
        # %%html, %%writefile, %%sql...: not Python, not shell. On Windows the path of
        # the file is written with backslashes.
        target = PurePosixPath(lines[0].split()[-1].replace("\\", "/"))
        if magic.group(1) in _FILE_CELL_MAGICS and target.suffix == ".py":
            view.written = {target.stem, *target.parts[:-1]}
        return view

    view.changes_folder = bool(_AUTOMAGIC_CD.search(text))
    python, commands = _without_magics(lines, skip_first=bool(magic))
    # A cell that is Python as it stands has no commands: a "!pip" line in valid
    # Python can only be inside a string.
    src = _parse_cell(text)
    if src is None:
        src = _parse_cell("\n".join(python))
        if src is None:
            view.commands = commands
            return view
        in_strings = {
            line
            for node in src.nodes
            if isinstance(node, _STRINGS)
            for line in range(node.lineno, node.end_lineno or node.lineno)
        }
        view.commands = [(offset, n) for offset, n in commands if n not in in_strings]
    view.ignores = {c.line: off for c in src.comments if (off := line_ignores(c.text))}
    if want_imports and not _LONE_CARRIAGE_RETURN.search(text):
        for name, lineno, byte_col in top_level_imports(src):
            line = lines[lineno - 1] if lineno <= len(lines) else ""
            view.imports.append((name, lineno, char_column(line, byte_col - 1)))
    return view


# --- What the engine uses ----------------------------------------------------------

# Text that says where packages come from, anywhere but in an index or links option
# of a ``pip install`` command that was read: "pip config set global.index-url ...",
# PIP_EXTRA_INDEX_URL, UV_INDEX, a pip.conf that the notebook writes or names, a
# login to a company's package store, the package that does the login.
_INDEX_SETTING = re.compile(
    r"index[-_]url|uv_(?:default_)?index|find[-_]links|pip\s+config|pip_config_file"
    r"|pip\.conf|pip\.ini|codeartifact|artifactregistry|keyring",
    re.I,
)


# Shell commands that install a package without a name that is read here, or fetch or
# unpack files that may be code: by their first word, or by their first two.
_FETCH_COMMANDS = frozenset({"wget", "curl", "unzip", "tar", "7z", "gdown"})
_FETCH_SUBCOMMANDS = frozenset(
    {
        ("git", "clone"),
        ("conda", "install"),
        ("mamba", "install"),
        ("uv", "add"),
        ("poetry", "add"),
        ("pipenv", "install"),
    }
)
# ``cp -r`` and ``cp -R``, also in a cluster of options (``-rf``).
_COPIES_A_FOLDER = re.compile(r"-[A-Za-z]*[rR][A-Za-z]*|--recursive")
# Commands that put a file in another place. One that names a module brings it in.
_COPY_COMMANDS = frozenset({"cp", "mv", "ln"})
# What ``<python> setup.py`` is told when it installs the folder's own package.
_SETUP_PY_INSTALLS = frozenset({"install", "develop"})
# Python text that changes where modules are found.
_MODULE_PATH = re.compile(r"sys\.path|PYTHONPATH|os\.chdir")


def _brings_code(words: list[Word], magic: bool) -> bool:
    """Whether a shell command may bring in modules that have no name here.

    It installs with a tool this module doesn't read (``conda install``, ``uv add``,
    ``python setup.py install``), fetches or unpacks files (``git clone``, ``wget``,
    ``unzip``, ``tar``), copies a folder or a module (``cp -r``, ``cp utils.py .``),
    or is ``%cd``: the modules of the new folder can be imported from then on.
    ``magic`` says that the line is a magic (``%``). A ``cd`` in a shell line changes
    nothing for the notebook.
    """
    names = [word.text for word in words]
    if names[:1] == ["cd"]:
        return magic
    if names[0] in _COPY_COMMANDS:
        folder = names[0] == "cp" and any(_COPIES_A_FOLDER.fullmatch(n) for n in names[1:])
        return folder or any(name.endswith(".py") for name in names[1:])
    if names[1:2] == ["setup.py"]:
        return bool(_SETUP_PY_INSTALLS.intersection(names[2:]))
    return names[0] in _FETCH_COMMANDS or tuple(names[:2]) in _FETCH_SUBCOMMANDS


def _offset(lines: list[str], lineno: int, col: int) -> int:
    """Offset in a cell's text of a 1-based line and 0-based character column."""
    return sum(len(line) + 1 for line in lines[: lineno - 1]) + col


def _installs(cell: Cell, view: CellView) -> Iterator[tuple[PipInstall, frozenset[str]]]:
    """Every ``pip install`` command of a cell, and every command that brings in code
    in another way (see ``_brings_code``): that one as an install that names nothing.
    So does a ``pip install`` in a form that is not read (``sudo pip install``), with
    the index and links options that it holds.

    Yields what the command says, and the rules that an ignore comment switches off
    for it. The comment may be at the end of the command, which is the end of its
    last line when it goes on over several. Or it is on a comment-only line right
    above the command: on Windows a shell line passes ``# ...`` on to pip, so there
    the comment can't share the line.
    """
    lines = cell.text.split("\n")
    for offset, n in view.commands:
        if len(lines[n]) > MAX_COMMAND_LENGTH:
            continue
        commands, end = shell_commands(cell.text, offset)
        last_line = cell.text[cell.text.rfind("\n", 0, end) + 1 : end]
        above = lines[n - 1].strip() if n else ""
        ignored = line_ignores(requirements_comment(last_line))
        if above.startswith("#"):
            ignored |= line_ignores(above)
        magic = cell.text[offset - 1 : offset] == "%"
        for words in commands:
            arguments = pip_install_arguments(words)
            if arguments is not None:
                yield pip_requirements(arguments), ignored
            elif unread := unread_pip_install(words):
                yield unread, ignored
            elif words and _brings_code(words, magic):
                yield PipInstall(unnamed=True), ignored


def _mentions_an_index(text: str, understood: list[tuple[int, int]]) -> bool:
    """Whether a cell's text names a package index anywhere but in the index and
    links options of ``pip install`` commands that were read (``understood``).

    The rest of such a command counts: ``pip install keyring`` and
    ``--keyring-provider`` are how a notebook logs in to a company's index.
    """
    return any(
        not any(start <= m.start() < end for start, end in understood)
        for m in _INDEX_SETTING.finditer(text)
    )


def scan_notebook(text: str, rel: str, want_imports: bool = False) -> DependencyFile:
    """What a notebook declares: the packages of its ``pip install`` lines and, with
    ``want_imports``, the imports of its code cells.

    The notebook's packages are private, all of them, when one of its commands
    names another source than PyPI, or when a cell sets an index in a way that
    can't be followed (``pip config``, an environment variable, a ``pip.conf``).
    Then no import is returned either. Nor is one when a command installs something
    without a name here (a path, a URL, a requirements file), when a command brings
    in code in another way (``git clone``, ``%conda install``, see ``_brings_code``),
    or when a cell changes where modules are found (``sys.path``, ``os.chdir``, a
    ``cd`` line): an
    import that nothing provides may come from there. So imports are only returned
    for a notebook that installs everything it uses by name. A module that a cell
    writes (``%%writefile utils.py``) is the notebook's own.

    ``# slopfence: ignore-file`` anywhere in a code cell covers the notebook. A cell
    may be a shell script, or not Python at all, so the words count wherever they
    are: there is no telling a comment from other text in every such cell.
    """
    found = DependencyFile(rel, Kind.NOTEBOOK)
    notebook = read_notebook(text)
    if notebook is None:
        return found
    ignore_file = any(IGNORE_FILE_RE.search(cell.text) for cell in notebook.cells)
    links = unnamed = False
    written: set[str] = set()
    for cell in notebook.cells:
        understood: list[tuple[int, int]] = []
        # Without a pip line, the cell needn't be parsed.
        if want_imports or "pip" in cell.text:
            view = view_cell(cell.text, want_imports)
            written |= view.written
            unnamed = unnamed or view.changes_folder
            for install, ignored in _installs(cell, view):
                understood += install.source_options
                found.private_index = found.private_index or install.private_index
                links = links or install.links
                unnamed = unnamed or install.unnamed
                for name, word, sourced in install.packages:
                    at = cell.text.find(name, word.start)
                    line, col = notebook.position(cell, at if at != -1 else word.start)
                    found.dependencies.append(
                        Dependency(
                            name,
                            rel,
                            line,
                            validate=not (sourced or install.variables),
                            ignored=ignored,
                            col=col,
                            own_source=sourced,
                        )
                    )
            lines = cell.text.split("\n")
            for name, lineno, col in view.imports:
                at = _offset(lines, lineno, col)
                off = view.ignores.get(lineno, frozenset())
                # Unusual line breaks: never report at a wrong place.
                if "SLOP001" not in off and cell.text.startswith(("import", "from"), at):
                    place = notebook.position(cell, at)
                    found.imports.append((name, *place))
                    if off:
                        found.import_ignores[place] = off
        if _mentions_an_index(cell.text, understood):
            found.private_index = True
        unnamed = unnamed or bool(want_imports and _MODULE_PATH.search(cell.text))
    private = found.private_index or links
    for dep in found.dependencies:
        dep.validate = dep.validate and not private
        if ignore_file:
            dep.ignored = ALL_RULES
    if private or ignore_file or unnamed:
        found.imports = []
    found.imports = [entry for entry in found.imports if entry[0] not in written]
    return found
