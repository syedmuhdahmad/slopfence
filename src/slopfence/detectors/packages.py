"""SLOP001: dependencies and imports that do not exist on PyPI.

Two checks:

* Declared dependencies are looked up by their distribution name. This is the
  most reliable signal. They are read from requirements files, pyproject.toml,
  setup.cfg, setup.py, Pipfile and ``pip install`` lines in Jupyter notebooks
  (see ``dependency.kind_of``).
* Imports in source code are opt-in (``check-imports``), because looking them
  up sends their names to PyPI and can reveal private package names. Even then
  they are only looked up when they are not stdlib, not part of the project,
  not installed, and not provided by a declared or locked distribution
  (``import yaml`` is provided by ``PyYAML``, see ``import_names``). Imports in
  the code cells of notebooks count too.

Nothing is looked up when the project or the environment may install packages
from a private index: a name missing from PyPI could then be a private package.
Nor is a name the project gives itself: a package that lives in the same
repository is not a hallucination.
"""

from __future__ import annotations

import codecs
import fnmatch
import importlib.metadata
import importlib.util
import json
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from slopfence.detectors.dependency import (
    INDEX_OPTION_STARTS,
    LINKS_OPTION_STARTS,
    MARKER_NAMES,
    REQ_COMMENT,
    Dependency,
    DependencyFile,
    Kind,
    egg_name,
    is_package_name,
    is_pip_option,
    kind_of,
    lists_other_packages,
    parse_requirement,
    requirements_comment,
    suppresses_slop001,
)
from slopfence.detectors.import_names import IMPORT_NAMES, provides
from slopfence.detectors.notebooks import MAX_BYTES, scan_notebook
from slopfence.detectors.setuptools_files import parse_setup_cfg, parse_setup_py
from slopfence.indexes import is_public_index, pipfile_is_private, uses_private_index
from slopfence.models import Finding
from slopfence.registry import BatchRegistry, OfflineRegistry, Registry, normalize
from slopfence.source import IGNORE_FILE_RE, load, read_source
from slopfence.source import relative_path as _rel

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
# Per-requirement options that may follow a requirement on its line (--hash=...). A
# match starts where a run of white space starts, so a long run is only walked once.
_LINE_OPTIONS = re.compile(r"(?<!\s)\s+--[A-Za-z].*$")
# How a line starts that is an option, a URL or a path, and so names no package.
_NOT_A_NAME = ("-", "git+", "hg+", "svn+", "bzr+", "http:", "https:", "file:", ".", "/")
_WORD_START = re.compile(r"[A-Za-z0-9]")
# The name starts and ends with something that is not white space, so the white space
# around it belongs to one part of the pattern only (see ``dependency._REQUIREMENT``).
_TABLE_HEADER = re.compile(r"^\s*\[\[?\s*([^\]\s](?:[^\]]*[^\]\s])?)\s*\]\]?\s*(#.*)?$")

# A Pipfile declares dependencies itself, so it is a dependency file, not a lock file.
LOCK_FILES = {"uv.lock", "poetry.lock", "pdm.lock", "Pipfile.lock"}
# Of the lock files, only this one records the indexes its packages come from.
INDEX_LOCK_FILES = {"Pipfile.lock"}
PIPFILE_TABLES = ("packages", "dev-packages")
# The keys of a Pipfile entry that comes from an index: Pipenv's own, and the PEP 508
# markers. An entry with any other key comes from somewhere else (git, path, file,
# url) or isn't understood (an unquoted dotted name makes a table inside a table).
_PIPFILE_INDEX_KEYS = MARKER_NAMES | {"version", "extras", "markers", "index", "editable"}
# The two files that setuptools reads as one distribution.
_SETUPTOOLS_KINDS = (Kind.SETUP_CFG, Kind.SETUP_PY)
# The kinds of file that say where an install gets its packages. A private index in
# one of them also covers the files that can't say (see ``DependencyFile.inherits_index``).
_INSTALL_KINDS = (Kind.REQUIREMENTS, Kind.PYPROJECT, Kind.PIPFILE)
# The byte order marks pip knows a requirements file by. The marks of UTF-32 come
# first: the little-endian one starts with the mark of UTF-16.
_BYTE_ORDER_MARKS = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
)
# The rules whose findings depend on answers from PyPI. A run that got no answer finds
# nothing for them, and that must never be read as "fixed". SLOP002 (#39) is named
# before it exists, so that the two rules can never get two lists.
PACKAGE_RULES = frozenset({"SLOP001", "SLOP002"})


@dataclass
class ProjectIndex:
    """What the project declares: its own modules, its dependency files, its locked packages."""

    root: Path
    local_modules: set[str] = field(default_factory=set)
    # Every dependency file that was read, in the order it was given.
    files: list[DependencyFile] = field(default_factory=list)
    # Distributions named in lock files (normalized), including indirect ones.
    locked: set[str] = field(default_factory=set)
    # Lock files that record a non-PyPI index.
    private_lock_files: list[str] = field(default_factory=list)

    @property
    def declared(self) -> set[str]:
        """Every declared name, including suppressed and non-PyPI ones."""
        return {normalize(dep.name) for file in self.files for dep in file.dependencies}

    @property
    def own_names(self) -> set[str]:
        """The names the project's own packages go by (normalized).

        They come from every ``pyproject.toml``, ``setup.cfg`` and ``setup.py`` of the
        project. A dependency with such a name is a package of the same repository,
        so it is never looked up, wherever it is declared.
        """
        return {normalize(name) for file in self.files for name in file.project_names}

    @property
    def sourced_names(self) -> set[str]:
        """The packages (normalized) that some file takes from a place of their own.

        ``corp-auth @ git+https://...`` in one file says where ``corp-auth`` comes
        from. The same name without the URL in another file (a ``setup.py``, a
        constraints file, a notebook) is the same package. So it is never looked up,
        wherever it is declared.
        """
        return {
            normalize(dep.name)
            for file in self.files
            for dep in file.dependencies
            if dep.own_source
        }

    @property
    def private_index_files(self) -> list[str]:
        """Project files that let packages come from a non-PyPI index."""
        return [file.rel for file in self.files if file.private_index] + self.private_lock_files

    @property
    def install_index_files(self) -> list[str]:
        """The private index files that say where an install gets its packages.

        These are requirements files, ``pyproject.toml``, ``Pipfile`` and
        ``Pipfile.lock``. A ``setup.py`` with ``dependency_links`` is not one: it only
        speaks for itself.
        """
        named = [f.rel for f in self.files if f.private_index and f.kind in _INSTALL_KINDS]
        return named + self.private_lock_files


def build_index(
    root: Path,
    py_files: Iterable[Path],
    dep_files: Iterable[Path],
    lock_files: Iterable[Path] = (),
    imports: bool = True,
) -> ProjectIndex:
    """Collect the project's own modules, its dependencies and its locked packages.

    Every dependency file is read once, and everything SLOP001 knows about it comes
    from that one reading. With ``imports`` False only the dependency check is
    prepared: the project's modules, the names in lock files and the imports in
    notebooks are left out, because only the import check asks for them.
    """
    index = ProjectIndex(root=root)
    for path in py_files if imports else ():
        index.local_modules.add(path.stem)
        for parent in path.parents:
            if parent == root or root not in parent.parents:
                break
            index.local_modules.add(parent.name)
    for path in dep_files:
        index.files.append(parse_dependency_file(path, root, imports))
    _share_setuptools_sources(index.files)
    for path in lock_files:
        if not imports and path.name not in INDEX_LOCK_FILES:
            continue  # a big lock file is only worth reading for its names
        names, private = parse_lock_file(path)
        index.locked |= {normalize(n) for n in names}
        if private:
            index.private_lock_files.append(_rel(path, root))
    return index


def _share_setuptools_sources(files: list[DependencyFile]) -> None:
    """Let a ``setup.cfg`` and the ``setup.py`` next to it agree on where packages come from.

    setuptools reads the two as one distribution. Links or an index named in one of
    them (``dependency_links``, ``[easy_install] index_url``) hold for the
    requirements in the other.
    """
    setuptools_files = [file for file in files if file.kind in _SETUPTOOLS_KINDS]
    private = {PurePosixPath(file.rel).parent for file in setuptools_files if file.private_index}
    for file in setuptools_files:
        if PurePosixPath(file.rel).parent in private:
            file.make_private()


def parse_lock_file(path: Path) -> tuple[set[str], bool]:
    """Distribution names in a lock file, and whether it uses a private index.

    Lock files list every installed package, including indirect dependencies a
    project may import directly (``import idna`` with only ``requests`` declared).
    """
    text = read_source(path)
    if text is None:
        return set(), False
    try:
        if path.name == "Pipfile.lock":
            data = json.loads(text)
            meta = data.get("_meta", {}) if isinstance(data, dict) else {}
            names = {n for group in ("default", "develop") for n in data.get(group, {})}
            return names, pipfile_is_private({"source": meta.get("sources", [])})
        data = tomllib.loads(text)
    # RecursionError: arrays nested thousands deep, in a file nobody wrote by hand.
    except (ValueError, AttributeError, TypeError, RecursionError):
        return set(), False
    # uv.lock, poetry.lock and pdm.lock all have [[package]] tables with a name.
    packages = _array(data.get("package"))
    names = {p["name"] for p in packages if isinstance(p, dict) and isinstance(p.get("name"), str)}
    return names, False


def toml_comments(text: str) -> list[str]:
    """The comment part of each line ("" if none), ignoring "#" inside TOML strings.

    Tracks multi-line strings across lines so quoted values can never carry
    a slopfence directive.
    """
    comments: list[str] = []
    multiline: str | None = None
    for line in text.split("\n"):
        comment, i = "", 0
        while i < len(line):
            if multiline:
                end = line.find(multiline, i)
                if end == -1:
                    break
                i, multiline = end + 3, None
                continue
            if line.startswith(('"""', "'''"), i):
                multiline, i = line[i : i + 3], i + 3
                continue
            ch = line[i]
            if ch in "\"'":
                j = i + 1
                while j < len(line) and line[j] != ch:
                    j += 2 if ch == '"' and line[j] == "\\" else 1
                i = j + 1
                continue
            if ch == "#":
                comment = line[i:]
                break
            i += 1
        comments.append(comment)
    return comments


def _is_too_large(path: Path) -> bool:
    """Whether a notebook is larger than ``notebooks.MAX_BYTES``, so it is not read."""
    try:
        return path.stat().st_size > MAX_BYTES
    except OSError:
        return False  # it can't be read at all, which the caller finds out next


def _unread_requirements_name_an_index(path: Path) -> bool:
    """Whether a requirements file that can't be read as UTF-8 names a private index.

    pip reads more than UTF-8: UTF-16 with a byte order mark (what ``>`` writes in
    Windows PowerShell), and the encoding of the user's locale. The packages of such
    a file stay unread, as in v0.2. Its index options are plain ASCII in each of
    these encodings, so they are found all the same, and the files that inherit from
    this one keep their names on the machine.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    mark, encoding = next(
        (found for found in _BYTE_ORDER_MARKS if data.startswith(found[0])), (b"", "latin-1")
    )
    text = data[len(mark) :].decode(encoding, "replace")
    return _requirements_sources(text.splitlines())[0]


def parse_dependency_file(path: Path, root: Path, imports: bool = False) -> DependencyFile:
    """What one dependency file of any kind declares. Empty when it can't be read.

    ``path`` is a file that ``kind_of`` calls a dependency file. Any other file is
    read as a requirements file, the one format that has no fixed name. With
    ``imports`` the imports in a notebook's code cells are collected too.
    """
    kind = kind_of(path, root) or Kind.REQUIREMENTS
    rel = _rel(path, root)
    if kind is Kind.SETUP_PY:
        # A Python file: parsed like any other, never executed.
        try:
            src = load(path, root)
        except (RecursionError, MemoryError):
            # Nested more deeply than Python's parser takes. Every setup.py of the
            # project is read here, also one that is excluded or not checked, so such
            # a file must not end the run. It declares nothing that can be read.
            src = None
        return parse_setup_py(src) if src is not None else DependencyFile(rel, kind)
    if kind is Kind.NOTEBOOK and _is_too_large(path):
        return DependencyFile(rel, kind, too_large=True)
    text = read_source(path)
    if text is None:
        private = kind is Kind.REQUIREMENTS and _unread_requirements_name_an_index(path)
        return DependencyFile(rel, kind, private_index=private)
    if kind is Kind.NOTEBOOK:
        return scan_notebook(text, rel, imports)
    if kind is Kind.SETUP_CFG:
        return parse_setup_cfg(text, rel)
    # Directives only count inside real comments, never inside values or URLs.
    if kind is Kind.REQUIREMENTS:
        found = _parse_requirements(text, rel)
        comments = [requirements_comment(line) for line in text.splitlines()]
    else:
        found = _parse_pyproject(text, rel) if kind is Kind.PYPROJECT else _parse_pipfile(text, rel)
        comments = toml_comments(text)
    if any(IGNORE_FILE_RE.search(c) for c in comments):
        for dep in found.dependencies:
            dep.suppressed = True
    return found


def _requirements_sources(lines: list[str]) -> tuple[bool, bool]:
    """Which package sources other than PyPI a requirements file names itself.

    The first answer is a non-PyPI ``--index-url`` or ``--extra-index-url``. The
    second is ``--find-links`` or ``--no-index``: no index, but a name that PyPI
    doesn't have may come from there. pip reads options from a line that starts
    with one, and such a line may hold several.
    """
    index = links = False
    for raw in lines:
        words = REQ_COMMENT.sub("", raw).split()
        if not words or not words[0].startswith("-"):
            continue
        for word, following in zip(words, [*words[1:], ""], strict=True):
            if is_pip_option(word, "-f", LINKS_OPTION_STARTS):
                links = True
            elif is_pip_option(word, "-i", INDEX_OPTION_STARTS):
                # The URL is attached ("--index-url=URL", "-iURL") or it is the next word.
                attached = word.partition("=")[2] if word.startswith("--") else word[2:]
                index = index or not is_public_index(attached or following)
    return index, links


def _parse_requirements(text: str, rel: str) -> DependencyFile:
    """Dependencies in a requirements file, with the line each one is on.

    A line must be a valid requirement. A file with more prose lines than
    requirements is a document that is only named like a requirements file, and
    yields nothing. Neither does a list for another package manager. The index
    options of the file count in every case.
    """
    lines = text.splitlines()
    # With another source than PyPI, a name missing from PyPI may be a private package.
    private_index, links = _requirements_sources(lines)
    if lists_other_packages(PurePosixPath(rel)):
        return DependencyFile(rel, Kind.REQUIREMENTS, private_index=private_index)
    deps = []
    valid = prose = 0
    # Whether any line uses pip's own syntax: an option, a URL, a version, an extra.
    pip_syntax = False
    for lineno, raw in enumerate(lines, start=1):
        suppressed = suppresses_slop001(requirements_comment(raw))
        line = REQ_COMMENT.sub("", raw).strip()
        if not line:
            continue
        if line.startswith(_NOT_A_NAME):
            pip_syntax = True
            # Options and bare URLs/paths: the name is only known from #egg=.
            if name := egg_name(line):
                deps.append(
                    Dependency(
                        name, rel, lineno, validate=False, suppressed=suppressed, own_source=True
                    )
                )
            continue
        spec = _LINE_OPTIONS.sub("", line.rstrip("\\").rstrip())
        parsed = parse_requirement(spec)
        if parsed is None:
            # "This file lists..." is prose; "{% if x %}" is templating and neither.
            prose += bool(_WORD_START.match(line))
            continue
        valid += 1
        pip_syntax = pip_syntax or not parsed.bare or spec != line
        deps.append(
            Dependency(
                parsed.name,
                rel,
                lineno,
                validate=not (parsed.direct or private_index or links),
                suppressed=suppressed,
                own_source=parsed.direct,
            )
        )
    found = DependencyFile(rel, Kind.REQUIREMENTS, private_index=private_index)
    if prose <= valid:
        found.dependencies = deps
        found.plain_list = valid > 0 and not pip_syntax
    return found


def _table_ranges(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Map each TOML table header to its (first, last) 0-based line range."""
    headers = [(i, m.group(1)) for i, line in enumerate(lines) if (m := _TABLE_HEADER.match(line))]
    ranges = {}
    for n, (start, name) in enumerate(headers):
        end = headers[n + 1][0] - 1 if n + 1 < len(headers) else len(lines) - 1
        ranges.setdefault(name.replace(" ", ""), (start, end))
    return ranges


class _LineFinder:
    """Find the physical line of each dependency entry within its own TOML table."""

    def __init__(self, text: str) -> None:
        """``text`` is the whole pyproject.toml."""
        self.lines = text.splitlines()
        self.comments = toml_comments(text)
        self.ranges = _table_ranges(self.lines)
        # (line, column) of entries already matched, so repeated specs map to
        # successive occurrences and several entries can share one line.
        self.used: set[tuple[int, int]] = set()

    def _array_range(self, start: int, end: int, key: str) -> tuple[int, int] | None:
        """Lines spanned by `key = [ ... ]` inside a table, using quote-aware bracket counting."""
        key_re = re.compile(r"""^\s*["']?""" + re.escape(key) + r"""["']?\s*=""")
        for i in range(start, end + 1):
            if not key_re.match(self.lines[i]):
                continue
            depth, seen_open = 0, False
            for j in range(i, end + 1):
                quote: str | None = None
                for ch in self.lines[j]:
                    if quote:
                        if ch == quote:
                            quote = None
                    elif ch in "\"'":
                        quote = ch
                    elif ch == "#":
                        break
                    elif ch == "[":
                        depth, seen_open = depth + 1, True
                    elif ch == "]":
                        depth -= 1
                if seen_open and depth <= 0:
                    return i, j
            return i, end
        return None

    def _search(self, table: str, pattern: re.Pattern[str], array: str | None = None) -> int:
        start, end = self.ranges.get(table, (0, len(self.lines) - 1))
        if array is not None and (span := self._array_range(start, end, array)):
            start, end = span
        for i in range(start, end + 1):
            line = self.lines[i]
            if line.lstrip().startswith("#"):  # commented-out entries
                continue
            for m in pattern.finditer(line):
                if (i, m.start()) not in self.used:
                    self.used.add((i, m.start()))
                    return i + 1
        return start + 1

    def spec(self, table: str, array: str, spec: str) -> int:
        quoted = re.compile(r"""(["'])""" + re.escape(spec) + r"\1")
        return self._search(table, quoted, array)

    def key(self, table: str, name: str) -> int:
        return self._search(
            table, re.compile(r"""^\s*["']?""" + re.escape(name) + r"""["']?\s*=""")
        )

    def suppressed(self, line: int) -> bool:
        """Whether an ignore comment on a line (1-based) covers SLOP001."""
        return 0 < line <= len(self.comments) and suppresses_slop001(self.comments[line - 1])


def _load_toml(text: str) -> dict | None:
    """The tables of a TOML file, or None when it can't be read.

    Not only a file that is not TOML: one with a number too long to convert, or with
    arrays nested thousands deep, can't be read either. Every ``pyproject.toml`` and
    ``Pipfile`` of the project is read, so no such file may end the run.
    """
    try:
        return tomllib.loads(text)
    except (ValueError, RecursionError):
        return None


def _table(value: object) -> dict:
    """``value`` if it is a TOML table, else an empty one.

    A file can be valid TOML and still have another shape than its format asks for
    (``project = "demo"``). Such a part is read as if it were not there.
    """
    return value if isinstance(value, dict) else {}


def _array(value: object) -> list:
    """``value`` if it is a TOML array, else an empty one (see ``_table``)."""
    return value if isinstance(value, list) else []


def _parse_pyproject(text: str, rel: str) -> DependencyFile:
    """Dependencies in pyproject.toml: PEP 621, dependency groups and Poetry tables."""
    found = DependencyFile(rel, Kind.PYPROJECT)
    data = _load_toml(text)
    if data is None:
        return found
    finder = _LineFinder(text)
    deps = found.dependencies
    project = _table(data.get("project"))
    project_name = normalize(project["name"]) if isinstance(project.get("name"), str) else None
    tool = _table(data.get("tool"))
    uv_sources = {normalize(n) for n in _table(_table(tool.get("uv")).get("sources"))}
    private_index = found.private_index = uses_private_index(tool)

    def add_spec(table: str, array: str, spec: object) -> None:
        """Record one PEP 508 requirement string from ``table.array``."""
        if not isinstance(spec, str) or not (m := _REQ_NAME.match(spec)):
            return
        name = m.group(1)
        if normalize(name) == project_name:  # self-references like "pkg[extra]"
            return
        line = finder.spec(table, array, spec)
        sourced = "://" in spec or normalize(name) in uv_sources
        validate = not (private_index or sourced)
        deps.append(
            Dependency(name, rel, line, validate, finder.suppressed(line), own_source=sourced)
        )

    for spec in _array(project.get("dependencies")):
        add_spec("project", "dependencies", spec)
    optional_table = (
        "project.optional-dependencies"
        if "project.optional-dependencies" in finder.ranges
        else "project"
    )
    for group, specs in _table(project.get("optional-dependencies")).items():
        array = group if optional_table != "project" else "optional-dependencies"
        for spec in _array(specs):
            add_spec(optional_table, array, spec)
    for group, specs in _table(data.get("dependency-groups")).items():
        for spec in _array(specs):
            add_spec("dependency-groups", group, spec)

    poetry = _table(tool.get("poetry"))
    # The project's own name: PEP 621, or Poetry's table in files written before it.
    found.project_names = {
        name for name in (project.get("name"), poetry.get("name")) if isinstance(name, str)
    }
    tables = {
        "tool.poetry.dependencies": _table(poetry.get("dependencies")),
        "tool.poetry.dev-dependencies": _table(poetry.get("dev-dependencies")),
    }
    for group, body in _table(poetry.get("group")).items():
        tables[f"tool.poetry.group.{group}.dependencies"] = _table(_table(body).get("dependencies"))
    for table, entries in tables.items():
        for name, spec in entries.items():
            if name.lower() == "python":
                continue
            specs = spec if isinstance(spec, list) else [spec]
            external = any(
                isinstance(s, dict) and {"path", "git", "url", "source"} & set(s) for s in specs
            )
            line = finder.key(table, name)
            validate = not (external or private_index) and is_package_name(name)
            deps.append(
                Dependency(name, rel, line, validate, finder.suppressed(line), own_source=external)
            )
    return found


def _pipfile_entry_is_public(spec: object, public: set[str]) -> bool:
    """Whether a Pipfile entry comes from PyPI, as far as the file says.

    ``public`` holds the names of the file's sources that are PyPI.
    """
    if isinstance(spec, str):
        return "://" not in spec
    if not isinstance(spec, dict) or not set(spec) <= _PIPFILE_INDEX_KEYS:
        return False
    index = spec.get("index")
    return index is None or (isinstance(index, str) and index in public)


def _parse_pipfile(text: str, rel: str) -> DependencyFile:
    """Dependencies in a Pipfile's ``[packages]`` and ``[dev-packages]`` tables.

    An entry that comes from git, a path, a URL or an index that is not one of the
    file's PyPI sources is declared but not looked up. So is one that isn't
    understood, and one whose key is not a package name.
    """
    found = DependencyFile(rel, Kind.PIPFILE)
    data = _load_toml(text)
    if data is None:
        return found
    finder = _LineFinder(text)
    private = found.private_index = pipfile_is_private(data)
    sources = data.get("source")
    # Pipenv's default source is PyPI, named "pypi".
    public = {"pypi"} if not sources else set()
    for source in sources if isinstance(sources, list) else []:
        if isinstance(source, dict) and is_public_index(source.get("url")):
            public.update(name for name in [source.get("name")] if isinstance(name, str))
    for table in PIPFILE_TABLES:
        entries = data.get(table)
        for name, spec in entries.items() if isinstance(entries, dict) else ():
            line = finder.key(table, name)
            from_index = _pipfile_entry_is_public(spec, public)
            found.dependencies.append(
                Dependency(
                    name,
                    rel,
                    line,
                    validate=from_index and not private and is_package_name(name),
                    suppressed=finder.suppressed(line),
                    own_source=not from_index,
                )
            )
    return found


def _installed_modules() -> set[str]:
    try:
        return set(importlib.metadata.packages_distributions())
    except Exception:  # pragma: no cover - broken environments
        return set()


def _is_installed(name: str, installed: set[str]) -> bool:
    if name in installed:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# A dependency file with some of its dependencies.
_FileDependencies = tuple[DependencyFile, list[Dependency]]


class PackageChecker:
    """Looks up dependencies and imports on PyPI and reports the ones that don't exist."""

    def __init__(
        self,
        index: ProjectIndex,
        registry: Registry,
        known_packages: Iterable[str] = (),
        private_index: str | None = None,
        inherited_from: Iterable[str] = (),
    ) -> None:
        """``private_index`` says where the environment configures a non-PyPI index, if anywhere.

        ``inherited_from`` are the project files whose private index also covers the
        dependency files that name no index themselves (see
        ``DependencyFile.inherits_index``). Empty when that is switched off.
        """
        self.index = index
        self.registry = registry
        # Private/internal packages the user vouches for; globs allowed ("corp-*").
        self._known = [normalize(p) for p in known_packages]
        # Where the environment configures a non-PyPI index (pip.conf, PIP_INDEX_URL...).
        self.private_index = private_index
        self.inherited_from = list(inherited_from)
        # Names that are never looked up, in whichever file they are declared.
        self._not_from_pypi = index.own_names | index.sourced_names
        self._installed = _installed_modules()
        self._provided_by = index.declared | index.locked
        self._missing: dict[str, bool] = {}
        # Import name -> the names to ask PyPI about ([] when it needs no lookup).
        self._candidates: dict[str, list[str]] = {}
        # Whether the registry had no answer for a name it was asked about.
        self._unanswered = False

    @property
    def imports_may_be_private(self) -> bool:
        """Whether any package may come from a private index, so unknown imports
        could be private packages and must not be sent to PyPI."""
        return bool(self.private_index or self.index.private_index_files)

    @property
    def _asks_nobody(self) -> bool:
        """Whether this run can't ask PyPI about anything: it is offline, or the
        environment configures a private index."""
        return bool(self.private_index) or isinstance(self.registry, OfflineRegistry)

    def _files(self, only_paths: set[str] | None) -> Iterator[DependencyFile]:
        """The dependency files that are checked: all of them, or the ones in ``only_paths``."""
        return (f for f in self.index.files if only_paths is None or f.rel in only_paths)

    def _may_be_public(self, dep: Dependency) -> bool:
        """Whether a declared dependency, by itself, is one PyPI can be asked about.

        Not one from a URL, a path or a private index, not one with an ignore
        comment, not a known package, not a package of the project's own, and not
        one that another file takes from a place of its own.
        """
        return (
            dep.validate
            and not dep.suppressed
            and not self._is_known(dep.name)
            and normalize(dep.name) not in self._not_from_pypi
        )

    def _inherits(self, file: DependencyFile) -> bool:
        """Whether a private index in another project file covers this file."""
        return file.inherits_index and bool(self.inherited_from)

    def _public_dependencies(self, only_paths: set[str] | None) -> Iterator[_FileDependencies]:
        """Each checked file with its dependencies that PyPI can be asked about.

        A file that has none is left out. So is one that a private index elsewhere in
        the project covers.
        """
        for file in self._files(only_paths):
            if self._inherits(file):
                continue
            public = [dep for dep in file.dependencies if self._may_be_public(dep)]
            if public:
                yield file, public

    def _lookup_dependencies(self, only_paths: set[str] | None) -> Iterator[_FileDependencies]:
        """Each checked file with its dependencies that are looked up on PyPI."""
        return iter(()) if self.private_index else self._public_dependencies(only_paths)

    def inherited_files(self, only_paths: set[str] | None = None) -> list[str]:
        """The checked files whose dependencies were not looked up, although nothing in
        the file itself said so: a private index in another project file covers them.

        Empty in a run that asks PyPI about nothing anyway.
        """
        if self._asks_nobody:
            return []
        return [
            file.rel
            for file in self._files(only_paths)
            if self._inherits(file) and any(self._may_be_public(dep) for dep in file.dependencies)
        ]

    def _lookup_names(self, name: str) -> list[str]:
        """The names to ask PyPI about for ``import name``: none if it needs no lookup.

        Every import name that reaches the registry comes from here, whether it is
        sent ahead in the batch or asked about by ``check_imports``.
        """
        if self._asks_nobody or self.index.private_index_files:
            return []
        if name not in self._candidates:
            # Sorted, so that requests go out in the same order on every run.
            found = sorted({name, name.replace("_", "-")}) if self._should_lookup(name) else []
            self._candidates[name] = found
        return self._candidates[name]

    def prefetch(self, only_paths: set[str] | None = None, imports: Iterable[str] = ()) -> None:
        """Hand the registry every name the two checks will ask about, as one batch.

        ``only_paths`` is what ``check_dependencies`` will get, and ``imports`` are the
        module names ``check_imports`` will get. The batch is built by the methods the
        checks use themselves, so it never holds a name they would not send. A
        registry that takes no batch is simply asked one name at a time later.
        """
        if not isinstance(self.registry, BatchRegistry):
            return
        names = [dep.name for _, deps in self._lookup_dependencies(only_paths) for dep in deps]
        for name in dict.fromkeys(imports):
            names.extend(self._lookup_names(name))
        if names:
            self.registry.prefetch(names)

    def _exists(self, name: str) -> bool | None:
        """What the registry says about ``name``. Remembers when it could not say."""
        found = self.registry.exists(name)
        if found is None:
            self._unanswered = True
        return found

    def lookups_unanswered(self, only_paths: set[str], imports: Iterable[str]) -> bool:
        """Whether this run may have missed findings because PyPI gave no answer.

        That is so when a name was asked about and the registry could not say: the run
        is offline, PyPI could not be reached, or it sent back something unexpected.
        It is also so when nobody was asked although there was a name to ask about.
        A name that is never looked up (a known package, the standard library, the
        project's own modules, anything declared) leaves nothing open.

        ``only_paths`` and ``imports`` are what the two checks were given: the
        dependency files of the run, and the modules its files import. Whether a
        module is installed is not looked at: an offline run has no reason to, and on
        another machine it may not be.
        """
        if self._unanswered or not self._asks_nobody:
            return self._unanswered
        # Offline, every dependency was still put to the registry, which had no answer:
        # that is recorded already. With a private index, none was.
        if self.private_index and any(self._public_dependencies(only_paths)):
            return True
        # A project whose own files use a private index never has its imports looked up.
        return not self.index.private_index_files and any(
            not self._is_provided(name) for name in imports
        )

    def check_dependencies(self, only_paths: set[str] | None = None) -> Iterator[Finding]:
        """Report declared dependencies that don't exist on PyPI.

        Nothing is reported for a file that may be a list for another tool (see
        ``DependencyFile.may_be_for_another_tool``) when PyPI has fewer of its names
        than it lacks. A list of apt or R packages looks like that. A pip file with
        one invented name among real ones does not. A name that PyPI gave no answer
        for may be missing too: it counts as lacking, so that a failed lookup can
        only make such a file quieter.
        """
        for file, deps in self._lookup_dependencies(only_paths):
            answers = [self._exists(dep.name) for dep in deps]
            missing = [dep for dep, found in zip(deps, answers, strict=True) if found is False]
            not_found = len(answers) - answers.count(True)
            if file.may_be_for_another_tool and not_found > answers.count(True):
                continue
            for dep in missing:
                yield Finding(
                    "SLOP001",
                    dep.path,
                    dep.line,
                    dep.col,
                    f"Dependency '{dep.name}' does not exist on PyPI (possible hallucination)",
                    subject=normalize(dep.name),
                )

    def _is_known(self, name: str) -> bool:
        key = normalize(name)
        return any(fnmatch.fnmatchcase(key, pattern) for pattern in self._known)

    def _is_provided(self, name: str) -> bool:
        """Whether the module comes from something the project itself names: a known
        package, Python, the project's own code, or a declared or locked package."""
        return (
            self._is_known(name)
            or name in sys.stdlib_module_names
            or name == "__future__"
            or name in sys.builtin_module_names
            or name in self.index.local_modules
            or name in IMPORT_NAMES
            or any(provides(dist, name) for dist in self._provided_by)
        )

    def _should_lookup(self, name: str) -> bool:
        """Whether an import needs a PyPI lookup: nothing local, installed or declared has it."""
        return not (self._is_provided(name) or _is_installed(name, self._installed))

    def _is_missing(self, name: str) -> bool:
        """Cached per name; every import occurrence is still reported separately."""
        if name not in self._missing:
            answers = [self._exists(c) for c in self._lookup_names(name)]
            self._missing[name] = bool(answers) and all(a is False for a in answers)
        return self._missing[name]

    def check_imports(self, rel: str, imports: Iterable[tuple[str, int, int]]) -> Iterator[Finding]:
        """Report imports that no installed, declared or PyPI package provides.

        ``imports`` are the (module, line, column) of the imports of the file ``rel``:
        see ``source.top_level_imports``.
        """
        for name, line, col in imports:
            if self._is_missing(name):
                yield Finding(
                    "SLOP001",
                    rel,
                    line,
                    col,
                    f"Import '{name}' is not installed, not declared, and no package with "
                    "that name exists on PyPI (possible hallucination)",
                    subject=name,
                )
