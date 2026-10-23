"""What every dependency parser shares (SLOP001, SLOP002).

* ``kind_of`` decides, from a file's name and folder, whether it declares
  dependencies and which parser reads it.
* ``Dependency`` is one declared package with its place in the file, and
  ``DependencyFile`` is everything one file declares.
* ``parse_requirement`` reads the package name out of a requirement string, and
  refuses everything that is not a requirement (URLs, paths, file names, prose).
  ``leading_name`` gives the name such a string starts with.
* ``is_pip_option`` knows pip's index and links options, also when they are cut short.
* ``text_lines`` cuts a text into the lines that git and an editor count.

Nothing here reads a file or asks the registry.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePath, PurePosixPath
from typing import NamedTuple


class Kind(str, Enum):
    """The kinds of file that declare dependencies. Each has its own parser."""

    PYPROJECT = "pyproject.toml"
    SETUP_CFG = "setup.cfg"
    SETUP_PY = "setup.py"
    PIPFILE = "Pipfile"
    NOTEBOOK = "notebook"
    REQUIREMENTS = "requirements file"


# The kinds that are known by their exact file name, in any folder.
_KIND_BY_NAME = {
    kind.value: kind for kind in (Kind.PYPROJECT, Kind.SETUP_CFG, Kind.SETUP_PY, Kind.PIPFILE)
}
NOTEBOOK_SUFFIX = ".ipynb"

REQUIREMENTS_SUFFIXES = (".txt", ".in")
REQUIREMENTS_WORDS = ("requirements", "constraints")
REQUIREMENTS_DIR = "requirements"
# "apt-requirements.txt" and "requirements/conda.txt" list packages for another
# package manager (see ``lists_other_packages``).
OTHER_PACKAGE_MANAGERS = frozenset(
    {
        "apk",
        "apt",
        "brew",
        "choco",
        "conda",
        "cran",
        "dnf",
        "gem",
        "npm",
        "pacman",
        "port",
        "rpm",
        "snap",
        "yum",
        "zypper",
    }
)
# Text files of other tools that a folder named "requirements" may hold.
NOT_REQUIREMENTS = frozenset({"CMakeLists.txt"})
_WORDS = re.compile(r"[a-z]+")

_OPERATOR = r"(?:===|==|~=|!=|<=|>=|<|>)"
_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_EXTRA = rf"(?:{_NAME}|\$\{{[A-Z0-9_]+\}})"
# A PEP 508 requirement: a name, optional extras, then nothing, a version specifier
# (which may stand in brackets), a marker or a direct reference. Anything else is not
# a requirement ("https://x/y.whl", "this is prose", "add_test(NAME x)", "2>&1"). The
# extras are names with commas between them, as pip reads them: "[security, socks]"
# is a list of extras, and bindep's "[platform:dpkg]" is not. An extra may be an
# environment variable, in the one form that pip expands before it reads the line:
# "[${EXTRAS}]". A version may start with "$" or "{": pip expands ${VAR}, and
# templates fill in {{ version }}. No two parts next to each other can match the same
# white space: if they could, a line with a long run of spaces would take hours to
# turn down.
_REQUIREMENT = re.compile(
    rf"""^\s*
    (?P<name>{_NAME})
    (?:\s*(?P<extras>\[\s*(?:{_EXTRA}\s*(?:,\s*{_EXTRA}\s*)*)?\]))?
    \s*(?P<rest>$|[;@].*$|(?:\(\s*)?{_OPERATOR}\s*[A-Za-z0-9*${{].*$)""",
    re.X | re.S,
)
# What a string starts with when it starts like a package name, as v0.2 read it.
_LEADING_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
# pip treats "#" as a comment only at line start or after whitespace (URLs contain "#egg=").
REQ_COMMENT = re.compile(r"(^|\s)#.*$")
# The name a URL gives its package: "git+https://host/repo.git#egg=name".
_EGG = re.compile(r"[#&]egg=([A-Za-z0-9][A-Za-z0-9._-]*)")
# A checksum on a line of its own is not a package name.
_DIGEST = re.compile(r"[0-9a-fA-F]{32,}")
_LETTER = re.compile(r"[A-Za-z]")
# A name with one of these endings is a file: "vendor.tar.gz", "requirements-dev.txt",
# "mypkg-1.0-py3-none-any.whl". ".py", ".egg" and ".yaml" are not here, because
# "discord.py", "zc.recipe.egg" and "ruamel.yaml" are packages.
_FILE_ENDINGS = (".whl", ".zip", ".gz", ".bz2", ".xz", ".tgz", ".txt", ".in", ".toml", ".cfg")


# The variables a PEP 508 marker may test (``python_version < "3.11"``).
MARKER_NAMES = frozenset(
    {
        "os_name",
        "sys_platform",
        "platform_machine",
        "platform_python_implementation",
        "platform_release",
        "platform_system",
        "platform_version",
        "python_version",
        "python_full_version",
        "implementation_name",
        "implementation_version",
        "extra",
    }
)


@dataclass
class Dependency:
    """One declared package and where it is declared."""

    name: str
    path: str
    line: int
    # False for packages that don't come from PyPI (URLs, paths, git, private indexes).
    validate: bool = True
    # The rules that an ignore directive switches off for this dependency: the ones it
    # names, or all of them for one that names none and for an ``ignore-file``.
    ignored: frozenset[str] = frozenset()
    # 1-based column of the requirement. Files with one requirement per line report 1.
    col: int = 1
    # True when the entry itself says where the package comes from: a URL, a path, git,
    # or a package source that it picks by name. That holds for the package, not just
    # for this entry: see ``ProjectIndex.sourced_names``.
    own_source: bool = False
    # False when the dependency does not inherit a private index set in another project
    # file.  Used for [build-system] requires so they stay on the machine when another
    # file names a private index.
    inherits_index: bool = True

    @property
    def suppressed(self) -> bool:
        """Whether an ignore directive covers SLOP001 here.

        The name is then never looked up, so SLOP002 can't report it either.
        """
        return "SLOP001" in self.ignored


@dataclass
class DependencyFile:
    """Everything one dependency file declares."""

    rel: str
    kind: Kind
    dependencies: list[Dependency] = field(default_factory=list)
    # What the file calls the project itself (``[project] name``, ``setup(name=...)``).
    project_names: set[str] = field(default_factory=set)
    # The file says that packages may come from a package index other than PyPI.
    private_index: bool = False
    # Requirements files only: no line uses pip's own syntax (a version, an extra, a
    # marker, an option). Such a file may be a list for another tool.
    plain_list: bool = False
    # Notebooks only: (top-level module, line, column) of every import in a code cell.
    # Empty unless they were asked for. An import under an ignore comment that covers
    # SLOP001 is left out: it is never looked up.
    imports: list[tuple[str, int, int]] = field(default_factory=list)
    # Notebooks only: the rules that an ignore comment switches off for an import, by
    # the (line, column) of that import. A line of the file can hold a whole cell, or
    # the whole notebook, so the line alone does not say which import is meant.
    import_ignores: dict[tuple[int, int], frozenset[str]] = field(default_factory=dict)
    # Notebooks only: the file is too large to be read (see ``notebooks.MAX_BYTES``).
    too_large: bool = False
    # ``setup.py``, ``setup.cfg`` and ``Pipfile`` only: the file lies under a test folder
    # of the project. The package it defines is a fixture, so its dependencies are not
    # looked up. Its own name and its private index count like those of any other file.
    fixture: bool = False
    # ``pyproject.toml`` and ``Pipfile`` only: the file could not be read. The TOML
    # reader turned it down (broken, or written in a newer TOML than it knows), it is
    # not UTF-8, or it can't be opened. It may set a private index that can't be seen
    # here.
    unreadable: bool = False

    def make_private(self) -> None:
        """Record that the file's packages may come from another place than PyPI: none
        of them is looked up."""
        self.private_index = True
        for dep in self.dependencies:
            dep.validate = False

    @property
    def read_by_v02(self) -> bool:
        """Whether v0.2 already read this file: ``pyproject.toml`` and ``requirements*.txt``.

        Every file that is read since v0.3 is treated with more care, so that the new
        version never sends a name the old one kept on the machine.
        """
        name = PurePosixPath(self.rel).name
        return self.kind is Kind.PYPROJECT or (
            self.kind is Kind.REQUIREMENTS
            and name.startswith("requirements")
            and name.endswith(".txt")
        )

    @property
    def inherits_index(self) -> bool:
        """Whether a private index in another project file also covers this file.

        The files v0.2 read speak for themselves: a private index counts for them when
        they name it. Every other file may be installed together with a file that
        names one (``pip install -r requirements.txt -c constraints.txt``), and most
        can't name an index at all. So one private index in the project covers them.
        """
        return not self.read_by_v02

    @property
    def may_be_for_another_tool(self) -> bool:
        """Whether the file may list packages for apt, R or another tool, not for pip.

        ``requirements/system.txt`` with one apt package per line looks exactly like
        a pip file. Only a plain list in a file that v0.2 did not read is doubted.
        """
        return self.plain_list and not self.read_by_v02


def is_requirements_file(path: PurePath) -> bool:
    """Whether a project-relative path is named like a pip requirements or constraints file.

    The name must end in ``.txt`` or ``.in``, and either

    * the name without that ending starts or ends with ``requirements`` or
      ``constraints`` (``requirements-dev.txt``, ``dev-requirements.txt``,
      ``requirements.in``, ``constraints.txt``), or
    * the file is inside a folder named ``requirements``, at any depth
      (``requirements/base.txt``, ``requirements/testing/all.in``).

    Matching is case-sensitive: ``REQUIREMENTS.txt`` is usually prose.
    ``CMakeLists.txt`` never counts. A file for another package manager
    (``apt-requirements.txt``) does, but its packages are not read: see
    ``lists_other_packages``.
    """
    name = path.name
    if not name.endswith(REQUIREMENTS_SUFFIXES) or name in NOT_REQUIREMENTS:
        return False
    stem = name.rsplit(".", 1)[0]
    if stem.startswith(REQUIREMENTS_WORDS) or stem.endswith(REQUIREMENTS_WORDS):
        return True
    return REQUIREMENTS_DIR in path.parts[:-1]


def lists_other_packages(path: PurePath) -> bool:
    """Whether a requirements file's name says its packages are for another package
    manager: the name holds one as a word (``apt-requirements.txt``,
    ``requirements/conda.txt``).

    Such names are not PyPI names, and nothing in the file tells them from pip's. So
    they are never read. An index option in the file is pip's all the same, and it
    counts like in any other requirements file.
    """
    stem = path.name.rsplit(".", 1)[0]
    return bool(OTHER_PACKAGE_MANAGERS.intersection(_WORDS.findall(stem.lower())))


def _kind_by_name(name: str) -> Kind | None:
    """The kind of a file whose folder doesn't matter: every kind but requirements files."""
    return Kind.NOTEBOOK if name.endswith(NOTEBOOK_SUFFIX) else _KIND_BY_NAME.get(name)


def dependency_kind(path: PurePath) -> Kind | None:
    """Which kind of dependency file a project-relative path is, or None if it is none."""
    kind = _kind_by_name(path.name)
    if kind is None and is_requirements_file(path):
        return Kind.REQUIREMENTS
    return kind


def kind_of(path: Path, root: Path) -> Kind | None:
    """``dependency_kind`` of a file, judged by its path inside the project ``root``.

    Folders above the project never count, so a project that lives in
    ``~/requirements/app`` doesn't turn every text file into a requirements file.
    A file outside ``root`` is judged by its name alone.
    """
    name = path.name
    if not name.endswith(REQUIREMENTS_SUFFIXES):
        # The folder doesn't matter, and most files of a project end up here. So no
        # relative path is worked out: on a big project that would double the walk.
        return _kind_by_name(name)
    try:
        rel = path.relative_to(root)
    except ValueError:
        try:
            rel = path.resolve().relative_to(root.resolve())
        except ValueError:
            rel = Path(name)
    return dependency_kind(rel)


class Requirement(NamedTuple):
    """What a requirement string says about the package it names."""

    name: str
    # The string says where the package comes from, so it never comes from an index:
    # ``name @ ...`` in any form, or a URL anywhere in it.
    direct: bool
    # The string is the name and nothing else: no extra, version, marker or URL.
    bare: bool


def parse_requirement(spec: str) -> Requirement | None:
    """Read a PEP 508 requirement string. None when ``spec`` is not a requirement.

    Not a requirement: a URL, a path, prose, a call like ``project(x)``, a file name
    (``vendor.tar.gz``), a checksum, a number or date, and ``python>=3.9`` (the
    interpreter is not a package).
    """
    m = _REQUIREMENT.match(spec)
    if not m:
        return None
    name = m.group("name")
    if (
        not _LETTER.search(name)
        or _DIGEST.fullmatch(name)
        or name.lower() == "python"
        or name.lower().endswith(_FILE_ENDINGS)
    ):
        return None
    rest = m.group("rest")
    # A URL in another place ("name==1.0 @ https://...") is no direct reference for
    # pip. Whoever wrote it meant that URL all the same.
    direct = rest.startswith("@") or "://" in spec
    return Requirement(name, direct, not (rest or m.group("extras")))


def leading_name(text: str) -> str | None:
    """The name that a string starts with, or None when it starts with none.

    For a string that is no requirement (``corp-auth=1.0``, ``corp-auth ^1.0``). Such
    a name is never looked up. It is declared all the same, as it was in v0.2: an
    ignore comment on it still counts, and the import of that package is not looked
    up either.
    """
    m = _LEADING_NAME.match(text)
    return m.group(1) if m else None


def is_package_name(text: str) -> bool:
    """Whether ``text`` is a package name and nothing else.

    A TOML key that names a package must be one. A quoted key can hold anything: a
    space, a path, a version. Such a key is no name to ask a registry about.
    """
    parsed = parse_requirement(text)
    return parsed is not None and parsed.name == text


def egg_name(url: str) -> str | None:
    """The package name in the ``#egg=`` part of a URL, or None if it has none."""
    m = _EGG.search(url)
    return m.group(1) if m else None


# pip's options that name a package index. pip also takes a long option cut short,
# down to the given start: "--extra-index" is one, and "--e" is not, because in a
# requirements file it could be "--editable" too.
INDEX_OPTION_STARTS = {"--index-url": "--i", "--extra-index-url": "--ex"}
# --find-links and --no-index add a package source too, whatever their value is.
LINKS_OPTION_STARTS = {"--find-links": "--f", "--no-index": "--no-i"}


def is_pip_option(word: str, short: str, long: Mapping[str, str]) -> bool:
    """Whether a word of a pip option line or command is one of the given options.

    ``short`` is the one-letter form, which may have its value attached
    (``-ihttps://...``). ``long`` maps each long form to the shortest start of it
    that pip still takes for it (``INDEX_OPTION_STARTS``, ``LINKS_OPTION_STARTS``).
    """
    if not word.startswith("--"):
        return word.startswith(short)
    name = word.partition("=")[0]
    return any(full.startswith(name) and name.startswith(start) for full, start in long.items())


def text_lines(text: str) -> list[str]:
    """The lines of a text, as git and an editor count them.

    A line ends at a line feed and nowhere else. ``str.splitlines`` also ends one at
    a form feed, U+0085 or U+2028, and every line after such a character then gets a
    number one too high. A carriage return before the line feed belongs to the line
    end. Every parser that reports a line number, or matches a comment to a line,
    takes its lines from here.
    """
    return [line.removesuffix("\r") for line in text.split("\n")]


def requirements_comment(line: str) -> str:
    """The comment part of a requirements-style line ("" if none)."""
    m = REQ_COMMENT.search(line)
    return m.group(0) if m else ""
