"""SLOP001: dependencies and imports that do not exist on PyPI.

Two checks:

* Declared dependencies (requirements*.txt, pyproject.toml) are looked up by
  their distribution name. This is the most reliable signal.
* Imports in source code are opt-in (``check-imports``), because looking them
  up sends their names to PyPI and can reveal private package names. Even then
  they are only looked up when they are not stdlib, not part of the project,
  not installed, and not provided by a declared or locked distribution
  (``import yaml`` is provided by ``PyYAML``, see ``import_names``).

Nothing is looked up when the project or the environment may install packages
from a private index: a name missing from PyPI could then be a private package.
"""

from __future__ import annotations

import fnmatch
import importlib.metadata
import importlib.util
import json
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from slopfence.detectors.import_names import IMPORT_NAMES, provides
from slopfence.indexes import is_public_index, pipfile_is_private, uses_private_index
from slopfence.models import Finding
from slopfence.registry import BatchRegistry, OfflineRegistry, Registry, normalize
from slopfence.source import IGNORE_FILE_RE, IGNORE_RE, read_source
from slopfence.source import relative_path as _rel

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_EGG = re.compile(r"[#&]egg=([A-Za-z0-9][A-Za-z0-9._-]*)")
# pip treats "#" as a comment only at line start or after whitespace (URLs contain "#egg=").
_REQ_COMMENT = re.compile(r"(^|\s)#.*$")
_INDEX_OPTION = re.compile(r"\s*(?:-i|--index-url|--extra-index-url)(?:\s*=\s*|\s+)(\S+)")
_TABLE_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


@dataclass
class Dependency:
    name: str
    path: str
    line: int
    # False for packages that don't come from PyPI (URLs, paths, git, private indexes).
    validate: bool = True
    # True when an ignore directive covers SLOP001 on this line or file.
    suppressed: bool = False


LOCK_FILES = {"uv.lock", "poetry.lock", "pdm.lock", "Pipfile.lock", "Pipfile"}


@dataclass
class ProjectIndex:
    root: Path
    local_modules: set[str] = field(default_factory=set)
    dependencies: list[Dependency] = field(default_factory=list)
    # Distributions named in lock files and Pipfiles (normalized), including indirect ones.
    locked: set[str] = field(default_factory=set)
    # Project files that let packages come from a non-PyPI index.
    private_index_files: list[str] = field(default_factory=list)

    @property
    def declared(self) -> set[str]:
        """Every declared name, including suppressed and non-PyPI ones."""
        return {normalize(d.name) for d in self.dependencies}


def build_index(
    root: Path,
    py_files: Iterable[Path],
    dep_files: Iterable[Path],
    lock_files: Iterable[Path] = (),
) -> ProjectIndex:
    """Collect the project's own modules, its dependencies and its locked packages."""
    index = ProjectIndex(root=root)
    for path in py_files:
        index.local_modules.add(path.stem)
        for parent in path.parents:
            if parent == root or root not in parent.parents:
                break
            index.local_modules.add(parent.name)
    for path in dep_files:
        index.dependencies.extend(parse_dependency_file(path, root))
        if dependency_file_is_private(path):
            index.private_index_files.append(_rel(path, root))
    for path in lock_files:
        names, private = parse_lock_file(path)
        index.locked |= {normalize(n) for n in names}
        if private:
            index.private_index_files.append(_rel(path, root))
    return index


def parse_lock_file(path: Path) -> tuple[set[str], bool]:
    """Distribution names in a lock file or Pipfile, and whether it uses a private index.

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
    except (ValueError, AttributeError, TypeError):
        return set(), False
    if path.name == "Pipfile":
        names = {n for group in ("packages", "dev-packages") for n in data.get(group, {}) or {}}
        return names, pipfile_is_private(data)
    # uv.lock, poetry.lock and pdm.lock all have [[package]] tables with a name.
    packages = data.get("package", [])
    names = {p["name"] for p in packages if isinstance(p, dict) and isinstance(p.get("name"), str)}
    return names, False


def dependency_file_is_private(path: Path) -> bool:
    """Whether a requirements file or pyproject.toml lets any package come from a
    non-PyPI index."""
    text = read_source(path)
    if text is None:
        return False
    if path.name == "pyproject.toml":
        try:
            return uses_private_index(tomllib.loads(text).get("tool", {}))
        except tomllib.TOMLDecodeError:
            return False
    return _requirements_use_private_index(text.splitlines())


def _suppresses_slop001(line: str) -> bool:
    m = IGNORE_RE.search(line)
    if not m:
        return False
    rules = m.group("rules")
    return rules is None or "SLOP001" in {r.strip().upper() for r in rules.split(",")}


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


def _requirements_comment(line: str) -> str:
    m = _REQ_COMMENT.search(line)
    return m.group(0) if m else ""


def parse_dependency_file(path: Path, root: Path) -> list[Dependency]:
    """The dependencies declared in a requirements file or pyproject.toml."""
    text = read_source(path)
    if text is None:
        return []
    rel = _rel(path, root)
    deps = (
        _parse_pyproject(text, rel)
        if path.name == "pyproject.toml"
        else _parse_requirements(text, rel)
    )
    # Directives only count inside real comments, never inside values or URLs.
    if path.name == "pyproject.toml":
        comments = toml_comments(text)
    else:
        comments = [_requirements_comment(line) for line in text.splitlines()]
    if any(IGNORE_FILE_RE.search(c) for c in comments):
        for dep in deps:
            dep.suppressed = True
    return deps


def _requirements_use_private_index(lines: list[str]) -> bool:
    """Whether a requirements file sets a non-PyPI ``--index-url`` or ``--extra-index-url``."""
    return any(
        (m := _INDEX_OPTION.match(_REQ_COMMENT.sub("", line))) and not is_public_index(m.group(1))
        for line in lines
    )


def _parse_requirements(text: str, rel: str) -> list[Dependency]:
    """Dependencies in a requirements file, with the line each one is on."""
    lines = text.splitlines()
    # With a non-PyPI index, a name missing from PyPI may be a private package.
    private_index = _requirements_use_private_index(lines)
    deps = []
    for lineno, raw in enumerate(lines, start=1):
        suppressed = _suppresses_slop001(_requirements_comment(raw))
        line = _REQ_COMMENT.sub("", raw).strip()
        if not line:
            continue
        if line.startswith(
            ("-", "git+", "hg+", "svn+", "bzr+", "http:", "https:", "file:", ".", "/")
        ):
            # Options and bare URLs/paths: the name is only known from #egg=.
            if m := _EGG.search(line):
                deps.append(
                    Dependency(m.group(1), rel, lineno, validate=False, suppressed=suppressed)
                )
            continue
        if m := _REQ_NAME.match(line):
            direct_url = "@" in line and "://" in line  # name @ https://...
            deps.append(
                Dependency(
                    m.group(1),
                    rel,
                    lineno,
                    validate=not (direct_url or private_index),
                    suppressed=suppressed,
                )
            )
    return deps


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
        return 0 < line <= len(self.comments) and _suppresses_slop001(self.comments[line - 1])


def _parse_pyproject(text: str, rel: str) -> list[Dependency]:
    """Dependencies in pyproject.toml: PEP 621, dependency groups and Poetry tables."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    finder = _LineFinder(text)
    deps: list[Dependency] = []
    project = data.get("project", {})
    project_name = normalize(project["name"]) if isinstance(project.get("name"), str) else None
    tool = data.get("tool", {})
    uv_sources = {normalize(n) for n in tool.get("uv", {}).get("sources", {})}
    private_index = uses_private_index(tool)

    def add_spec(table: str, array: str, spec: object) -> None:
        """Record one PEP 508 requirement string from ``table.array``."""
        if not isinstance(spec, str) or not (m := _REQ_NAME.match(spec)):
            return
        name = m.group(1)
        if normalize(name) == project_name:  # self-references like "pkg[extra]"
            return
        line = finder.spec(table, array, spec)
        validate = not private_index and "://" not in spec and normalize(name) not in uv_sources
        deps.append(Dependency(name, rel, line, validate, finder.suppressed(line)))

    for spec in project.get("dependencies", []):
        add_spec("project", "dependencies", spec)
    optional_table = (
        "project.optional-dependencies"
        if "project.optional-dependencies" in finder.ranges
        else "project"
    )
    for group, specs in project.get("optional-dependencies", {}).items():
        array = group if optional_table != "project" else "optional-dependencies"
        for spec in specs:
            add_spec(optional_table, array, spec)
    for group, specs in data.get("dependency-groups", {}).items():
        for spec in specs:
            add_spec("dependency-groups", group, spec)

    poetry = tool.get("poetry", {})
    tables = {
        "tool.poetry.dependencies": poetry.get("dependencies", {}),
        "tool.poetry.dev-dependencies": poetry.get("dev-dependencies", {}),
    }
    for group, body in poetry.get("group", {}).items():
        tables[f"tool.poetry.group.{group}.dependencies"] = body.get("dependencies", {})
    for table, entries in tables.items():
        for name, spec in entries.items():
            if name.lower() == "python":
                continue
            specs = spec if isinstance(spec, list) else [spec]
            external = any(
                isinstance(s, dict) and {"path", "git", "url", "source"} & set(s) for s in specs
            )
            line = finder.key(table, name)
            validate = not (external or private_index)
            deps.append(Dependency(name, rel, line, validate, finder.suppressed(line)))
    return deps


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


class PackageChecker:
    """Looks up dependencies and imports on PyPI and reports the ones that don't exist."""

    def __init__(
        self,
        index: ProjectIndex,
        registry: Registry,
        known_packages: Iterable[str] = (),
        private_index: str | None = None,
    ) -> None:
        """``private_index`` says where the environment configures a non-PyPI index, if anywhere."""
        self.index = index
        self.registry = registry
        # Private/internal packages the user vouches for; globs allowed ("corp-*").
        self._known = [normalize(p) for p in known_packages]
        # Where the environment configures a non-PyPI index (pip.conf, PIP_INDEX_URL...).
        self.private_index = private_index
        self._installed = _installed_modules()
        self._provided_by = index.declared | index.locked
        self._missing: dict[str, bool] = {}
        # Import name -> the names to ask PyPI about ([] when it needs no lookup).
        self._candidates: dict[str, list[str]] = {}

    @property
    def imports_may_be_private(self) -> bool:
        """Whether any package may come from a private index, so unknown imports
        could be private packages and must not be sent to PyPI."""
        return bool(self.private_index or self.index.private_index_files)

    def _lookup_dependencies(self, only_paths: set[str] | None) -> Iterator[Dependency]:
        """The declared dependencies that are looked up on PyPI."""
        if self.private_index:
            return
        for dep in self.index.dependencies:
            if only_paths is not None and dep.path not in only_paths:
                continue
            if not dep.validate or dep.suppressed or self._is_known(dep.name):
                continue
            yield dep

    def _lookup_names(self, name: str) -> list[str]:
        """The names to ask PyPI about for ``import name``: none if it needs no lookup.

        Every import name that reaches the registry comes from here, whether it is
        sent ahead in the batch or asked about by ``check_imports``.
        """
        if isinstance(self.registry, OfflineRegistry) or self.imports_may_be_private:
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
        names = [dep.name for dep in self._lookup_dependencies(only_paths)]
        for name in dict.fromkeys(imports):
            names.extend(self._lookup_names(name))
        if names:
            self.registry.prefetch(names)

    def check_dependencies(self, only_paths: set[str] | None = None) -> Iterator[Finding]:
        """Report declared dependencies that don't exist on PyPI."""
        for dep in self._lookup_dependencies(only_paths):
            if self.registry.exists(dep.name) is False:
                yield Finding(
                    "SLOP001",
                    dep.path,
                    dep.line,
                    1,
                    f"Dependency '{dep.name}' does not exist on PyPI (possible hallucination)",
                )

    def _is_known(self, name: str) -> bool:
        key = normalize(name)
        return any(fnmatch.fnmatchcase(key, pattern) for pattern in self._known)

    def _should_lookup(self, name: str) -> bool:
        """Whether an import needs a PyPI lookup: nothing local, installed or declared has it."""
        return not (
            self._is_known(name)
            or name in sys.stdlib_module_names
            or name == "__future__"
            or name in sys.builtin_module_names
            or name in self.index.local_modules
            or name in IMPORT_NAMES
            or any(provides(dist, name) for dist in self._provided_by)
            or _is_installed(name, self._installed)
        )

    def _is_missing(self, name: str) -> bool:
        """Cached per name; every import occurrence is still reported separately."""
        if name not in self._missing:
            answers = [self.registry.exists(c) for c in self._lookup_names(name)]
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
                )
