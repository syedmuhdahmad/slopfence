"""SLOP001 in setup.cfg, setup.py, Pipfile and more requirements file names (#53)."""

import codecs
import configparser
import json
import random
import time
from pathlib import PurePosixPath

import pytest

from slopfence.cli import main
from slopfence.detectors.dependency import (
    Kind,
    dependency_kind,
    kind_of,
    parse_requirement,
    text_lines,
)
from slopfence.detectors.packages import parse_dependency_file
from slopfence.detectors.setuptools_files import _cfg_lines
from slopfence.engine import discover_all, run
from slopfence.registry import OfflineRegistry, PyPIRegistry
from slopfence.source import relative_path
from tests.conftest import DownRegistry, FakeRegistry, needs_parser_depth_limit

PRIVATE = "https://user:secret@pkgs.corp.example/simple"
INDEX = f"--extra-index-url {PRIVATE}\n"
GIT = "git+https://git.corp.example/corp/corp-auth.git"


def found(result):
    """(path, line, column) of every finding."""
    return [(f.path, f.line, f.col) for f in result.findings]


def brief(path, root):
    """(name, line, validate) of each dependency the file declares."""
    return [(d.name, d.line, d.validate) for d in parse_dependency_file(path, root).dependencies]


# --- Which files count ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "requirements.txt",
        "requirements-dev.txt",
        "requirements_test.txt",
        "requirements3.txt",
        "dev-requirements.txt",
        "linters.requirements.txt",
        "requirements.in",
        "dev-requirements.in",
        "constraints.txt",
        "constraints-3.8.txt",
        "package_constraints.txt",
        "requirements/base.txt",
        "requirements/dev.in",
        "tests/requirements/py3.txt",
        "requirements/static/ci/py3.10/linux.in",
        # A word that only holds a package manager's name is not that package manager.
        "support-requirements.txt",
        "requirements-snapshot.txt",
        # A list for another package manager: only its index options are read.
        "apt-requirements.txt",
        "requirements/system/ubuntu/apt-packages.txt",
    ],
)
def test_requirements_file_names(name):
    """Names that pip-tools, Django-style layouts and constraints files use."""
    assert dependency_kind(PurePosixPath(name)) is Kind.REQUIREMENTS


@pytest.mark.parametrize(
    "name",
    [
        "requirements.md",
        "requirements.yml",
        "requirements.txt.template",
        "requirements.lock",
        "REQUIREMENTS.txt",
        "requires.txt",
        "README.txt",
        "my-requirements-notes.txt",
        "MANIFEST.in",
        "proctoringrequirements/email/body.txt",
        # A build file of another tool in a folder that happens to be called requirements.
        "tests/requirements/CMakeLists.txt",
        "Pipfile.lock",
        "my_setup.py",
        "setup.cfg.bak",
    ],
)
def test_names_that_are_not_dependency_files(name):
    """Docs, templates, lock files and other tools' build files are left alone."""
    assert dependency_kind(PurePosixPath(name)) is None


OTHER_MANAGERS = [
    "apt-requirements.txt",
    "requirements-conda.txt",
    "requirements_apt.txt",
    "requirements/system/ubuntu/apt-packages.txt",
    "requirements-cran.txt",
    "choco-requirements.txt",
    "requirements/snap.txt",
]


@pytest.mark.parametrize("name", OTHER_MANAGERS)
def test_the_packages_of_a_list_for_another_package_manager_are_not_read(
    write, check, tmp_path, name
):
    """``apt-requirements.txt`` lists apt packages. They are not PyPI names, so none
    is declared, sent or reported."""
    path = write(name, "libxml2-dev\nbuild-essential>=12\n")
    write("constraints.txt", "ghost-pin==1.0\n")
    assert parse_dependency_file(path, tmp_path).dependencies == []
    registry = FakeRegistry(set())
    assert found(check("SLOP001", registry=registry)) == [("constraints.txt", 1, 1)]
    assert registry.queries == ["ghost-pin"]


@pytest.mark.parametrize("name", OTHER_MANAGERS)
def test_an_index_in_a_list_for_another_package_manager_still_counts(
    write, check, name, not_installed
):
    """A file with an index option is a pip file, whatever its name says. v0.2 read
    ``requirements-conda.txt`` and kept the project's names on the machine: the files
    that inherit are not looked up, and neither are imports."""
    write(name, INDEX + "corp-auth==1.0\n")
    write("setup.py", "from setuptools import setup\nsetup(install_requires=['corp-core'])\n")
    write("src/app.py", "import corp_models\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, check_imports=True)
    assert result.findings == [] and registry.queries == []
    assert f"{name} uses a private package index" in result.notes[0]
    assert f"a private package index is set in {name}" in result.notes[1]


def test_other_kinds():
    """setup.py, setup.cfg, Pipfile and pyproject.toml are known by name, in any folder."""
    names = ["a/setup.py", "setup.cfg", "b/c/Pipfile", "pyproject.toml"]
    assert [dependency_kind(PurePosixPath(name)) for name in names] == [
        Kind.SETUP_PY,
        Kind.SETUP_CFG,
        Kind.PIPFILE,
        Kind.PYPROJECT,
    ]


def test_discovery(write, tmp_path):
    """setup.py is a Python file and a dependency file, and a ``requirements`` folder
    above the project doesn't count."""
    # Resolved, as the paths that come back are: a temporary folder can be a link.
    root = (tmp_path / "requirements" / "app").resolve()
    for rel in [
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "notes.txt",
        "requirements/base.txt",
        "uv.lock",
    ]:
        write(f"requirements/app/{rel}", "")
    py, deps, locks = discover_all([root], root)
    assert [p.name for p in py] == ["setup.py"]
    assert sorted(p.relative_to(root).as_posix() for p in deps) == [
        "Pipfile",
        "requirements/base.txt",
        "setup.cfg",
        "setup.py",
    ]
    assert [p.name for p in locks] == ["uv.lock"]
    # Explicit files, as pre-commit passes them.
    assert discover_all([root / "notes.txt", root / "setup.cfg"], root)[1] == [root / "setup.cfg"]
    # A file outside the project is judged by its name alone.
    assert kind_of(write("elsewhere/requirements/notes.txt", ""), root) is None
    assert kind_of(write("elsewhere/requirements.txt", ""), root) is Kind.REQUIREMENTS


# --- Requirement strings ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("requests", ("requests", False, True)),
        ("  requests  ", ("requests", False, True)),
        ("Flask[async] >= 2 ; python_version < '3.11'", ("Flask", False, False)),
        ("celery[redis]", ("celery", False, False)),
        # Extras as pip takes them: names with commas between them, or none.
        ("requests[security, socks]>=2", ("requests", False, False)),
        ("requests [ security , socks_v5 ]", ("requests", False, False)),
        ("requests[]", ("requests", False, False)),
        # pip fills in an environment variable before it reads the line.
        ("ghost-lib[${EXTRAS}]==1.0", ("ghost-lib", False, False)),
        ("apache-airflow[celery, ${AIRFLOW_EXTRAS}]", ("apache-airflow", False, False)),
        (
            "corp-lib[${EXTRAS}] @ git+https://git.corp.example/corp-lib.git",
            ("corp-lib", True, False),
        ),
        ("zope.interface (>=5.0)", ("zope.interface", False, False)),
        ("corp-lib @ https://example.com/corp.whl", ("corp-lib", True, False)),
        # A direct reference in every form: a path, a Hatch context, no space, a URL
        # in a place where pip does not take one.
        ("corp-lib @ file:../corp-lib", ("corp-lib", True, False)),
        ("corp-lib @ ./vendor/corp-lib", ("corp-lib", True, False)),
        ("corp-lib @ {root:parent:uri}/corp-lib", ("corp-lib", True, False)),
        ("corp-lib@git+ssh://git@corp.example/corp-lib.git", ("corp-lib", True, False)),
        ("corp-lib==1.0 @ https://example.com/corp_lib-1.0.whl", ("corp-lib", True, False)),
        ("corp-lib>=1.0 https://example.com/corp_lib-1.0.whl", ("corp-lib", True, False)),
        ("pkg===1.0.custom", ("pkg", False, False)),
        ("templated==${VERSION}", ("templated", False, False)),
        ("Django>={{ django_version }}", ("Django", False, False)),
        # Packages whose names end like a file.
        ("discord.py", ("discord.py", False, True)),
        ("ruamel.yaml>=0.17", ("ruamel.yaml", False, False)),
    ],
)
def test_requirement_strings(spec, expected):
    """A requirement gives its name, whether it is a direct reference, and whether it
    is the name alone."""
    assert parse_requirement(spec) == expected


@pytest.mark.parametrize(
    "spec",
    [
        "https://example.com/pkg-1.0.whl",
        "./local/dir",
        "this is prose",
        "typo = 1.0",
        "2>&1",
        "f80db442347ca0dd7d7ca368405ffeee",
        "python>=3.9",
        "",
        # A call in a CMake file is not a requirement with a bracketed version.
        "cmake_minimum_required(VERSION 3.16)",
        "add_test(NAME requires_clause COMMAND requires_clause)",
        # A bracket that is no list of extras: bindep's platform selector.
        "build-essential [platform:dpkg]",
        "libxml2-dev [platform:dpkg test]",
        "requests[security socks]",
        "requests[security,]",
        "requests[{{ extras }}]",
        # pip expands ${VAR} in capitals and nothing else.
        "requests[${extras}]",
        "requests[$EXTRAS]",
        "requests[${EXTRAS} ${MORE}]",
        # File names.
        "mypkg-1.0-py3-none-any.whl",
        "vendor.tar.gz",
        "requirements-dev.txt",
        "base.in",
        # Numbers, dates and addresses.
        "2024-01-01",
        "10.0.0.1",
        "3.14",
    ],
)
def test_strings_that_are_not_requirements(spec):
    """URLs, paths, prose, calls, file names, checksums and numbers give no name."""
    assert parse_requirement(spec) is None


# --- Requirements files -----------------------------------------------------------------


def test_new_requirements_names_found_and_missing(write, check):
    """.in files, constraints and requirements/ folders are checked like requirements.txt."""
    write("requirements/dev.in", "requests\nghost-dev  # a comment\n")
    write("constraints.txt", "requests==2.31\nghost-pin==1.0\n")
    write("dev-requirements.txt", "ghost-tool>=1  # slopfence: ignore\n")
    registry = FakeRegistry({"requests"})
    result = check("SLOP001", registry=registry)
    assert found(result) == [("constraints.txt", 2, 1), ("requirements/dev.in", 2, 1)]
    assert [f.subject for f in result.findings] == ["ghost-pin", "ghost-dev"]
    assert "ghost-tool" not in registry.queries  # an ignore comment: never sent
    assert result.files_checked == 3 and result.notes == []


@pytest.mark.parametrize("name", ["requirements/dev.in", "constraints.txt", "dev-requirements.txt"])
def test_new_requirements_names_with_their_own_private_index(write, check, name):
    """An index in the file itself makes its packages private, as in requirements.txt."""
    write(name, INDEX + "corp-lib==1.0\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert result.findings == [] and registry.queries == []
    assert result.notes == []  # the file says so itself: nothing to explain


def test_requirement_lines_must_be_requirements(tmp_path, write):
    """Options, URLs, templating and broken lines never give a name to look up. The
    name that a broken line starts with is declared all the same, as it was in v0.2."""
    path = write(
        "requirements.txt",
        """
        requests>=2.31
        hashed==1.0 \\
            --hash=sha256:abc
        opt-pkg>=1 --global-option="--no-user-cfg"
        Django>={{ django_version }}
        {% if use_x %}
        git+https://github.com/org/repo.git#egg=from-git
        direct @ https://example.com/d.whl
        typo = 1.0
        src/local-package
        python >= 3.9
        mypkg-1.0-py3-none-any.whl
        """,
    )
    assert brief(path, tmp_path) == [
        ("requests", 1, True),
        ("hashed", 2, True),
        ("opt-pkg", 4, True),
        ("Django", 5, True),
        ("from-git", 7, False),
        ("direct", 8, False),
        ("typo", 9, False),
        ("src", 10, False),
        ("python", 11, False),
        ("mypkg-1.0-py3-none-any.whl", 12, False),
    ]


def test_extras_from_an_environment_variable_keep_a_line_a_requirement(write, check):
    """``name[${EXTRAS}]`` is a requirement for pip. So the invented name on such a line
    is reported, and a package that such a line takes from git is not looked up in
    the file that names it again."""
    write("requirements.txt", f"requests\nghost-lib[${{EXTRAS}}]==1.0\ncorp-auth[${{X}}] @ {GIT}\n")
    write("setup.cfg", "[options]\ninstall_requires =\n    corp-auth\n")
    registry = FakeRegistry({"requests"})
    assert found(check("SLOP001", registry=registry)) == [("requirements.txt", 2, 1)]
    assert registry.queries == ["requests", "ghost-lib"]


SPACES = " " * 5000
LONG = [
    # Not a requirement: every way to cut the line up is tried before it is turned down.
    ("requirements.txt", f"requests\nflask{SPACES}extra words\n"),
    ("constraints.txt", f"requests==2.31\nflask{SPACES}> =1\n"),
    # No per-requirement option after the white space.
    ("requirements.txt", "requests\nflask" + "\t" * 100_000 + "-x\n"),
    # A bracket after the name that is no list of extras.
    ("requirements.txt", f"requests\nflask[{SPACES}async now]\n"),
    ("constraints.txt", "requests==2.31\nflask[" + f"async{SPACES}," * 50 + "!]\n"),
    ("setup.cfg", f"[options]\ninstall_requires =\n    requests\n    flask{SPACES}extra words\n"),
    # A line of a TOML string that starts like a table header.
    (
        "pyproject.toml",
        '[project]\nname = "demo"\ndependencies = ["requests"]\n'
        f'description = """\n[{SPACES}x\n"""\n',
    ),
    ("Pipfile", f'[packages]\nrequests = "*"\n\n[scripts]\nrun = """\n[[{SPACES}x\n"""\n'),
]


@pytest.mark.parametrize(("name", "text"), LONG, ids=[f"{n} {i}" for i, (n, _) in enumerate(LONG)])
def test_a_long_run_of_white_space_is_read_in_no_time(tmp_path, name, text):
    """A line with thousands of spaces is walked once, not once per space. When two
    parts of a pattern could match the same spaces, a file of a few kilobytes took
    minutes: this test would then run for as long."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    start = time.perf_counter()
    declared = parse_dependency_file(path, tmp_path).dependencies
    assert time.perf_counter() - start < 5
    assert [dep.name for dep in declared if dep.validate] == ["requests"]


def test_a_long_setup_cfg_line_is_split_in_no_time():
    """The option name of a setup.cfg line ends at the first ``=`` or ``:``, however
    much white space the line holds before it."""
    line = "install" + " " * 100_000 + "requires = requests"
    start = time.perf_counter()
    lines = list(_cfg_lines(f"[options]\n{line}\n"))
    assert time.perf_counter() - start < 5
    assert lines == [("options", "install" + " " * 100_000 + "requires", (2, "requests", True))]


def test_prose_named_like_requirements_is_skipped(write, check):
    """A document that is only named like a requirements file yields nothing."""
    write(
        "docs/constraints.txt",
        """
        Constraints
        ===========

        This document describes the database constraints.
        Use them with care.
        They are checked on save.
        requests
        """,
    )
    write("output-requirements.txt", "f80db442347ca0dd7d7ca368405ffeee\n")
    registry = FakeRegistry(set())
    assert check("SLOP001", registry=registry).findings == []
    assert registry.queries == []


# Documents that are only named like a requirements file. Their one-word lines read
# like package names.
DOCUMENTS = {
    # A Sphinx index kept as a text file.
    "docs/requirements/index.txt": (
        "Requirements\n============\n\n.. toctree::\n   :maxdepth: 1\n\n"
        "   hardware\n   software\n   network\n"
    ),
    "system-requirements.txt": (
        "Minimum\n-------\n- 4 GB RAM\n- Ubuntu 20.04 or later\n- Python 3.8+\n\n"
        "Recommended\n-----------\n- 16 GB RAM\n- NVIDIA GPU with CUDA 11\n"
    ),
    "hardware-requirements.txt": "Hardware\n\n- 4 GB RAM\n- 2 CPUs\n\nSoftware\n\n- Ubuntu\n",
    "requirements/README.txt": "Overview\n--------\n\nbase\n  at run time\ndev\n  for the tests\n",
    "requirements.txt": "Usage\n-----\n\ninstall\n\n... then start the server\n",
    # The same kinds of document with other markup: "=" and "~" underlines, "*" and
    # "+" bullets, a quote, a table, literal text, an editor's mode line.
    "docs/requirements/install.txt": (
        "Requirements\n============\n\n* Python 3.8 or later\n* 4 GB RAM\n\n"
        "Installation\n============\n\n1. Clone the repository\n2. Run make\n"
    ),
    "network-requirements.txt": (
        "Hardware\n========\n\n* 4 GB RAM\n\nSoftware\n========\n\n* Ubuntu 20.04\n\n"
        "Network\n=======\n\n* Port 443 open\n"
    ),
    "os-requirements.txt": "# Needs\n\nHardware\n\n+ 4 GB RAM\n\nSoftware\n\n> Ubuntu only\n",
    "docs/requirements/usage.txt": "Overview\n~~~~~~~~\n\nUsage\n~~~~~\n",
    "requirements/NOTES.txt": "Requirements\n\n| name | version |\n\nNotes\n\n``pip install``\n",
    "requirements/index.txt": ".. -*- mode: rst -*-\n\nbase\ndev\n",
    # Not a document, and no file pip takes either: a merge that is not finished.
    "requirements-dev.txt": "<<<<<<< HEAD\nbase==2.0\n=======\nghost-lib==1.0\n>>>>>>> dev\n",
}
# Some words are missing on purpose: with as many names found as not, a file that is
# read as a plain list is reported.
WORDS = {"requirements", "hardware", "software", "minimum", "overview", "base", "usage"}


@pytest.mark.parametrize("name", sorted(DOCUMENTS))
@pytest.mark.parametrize("on_pypi", [set(), WORDS], ids=["no word on PyPI", "most words on PyPI"])
def test_a_document_with_an_underline_a_bullet_or_a_directive_is_skipped(
    write, check, name, on_pypi
):
    """A line that starts like markup ("-", ".", "=", "~", "*", "+", ">", "|", a
    backquote) and is no option and no path is the markup of a document: pip reads
    no package from such a line. Nothing of the file is sent or reported, whatever
    PyPI has."""
    write(name, DOCUMENTS[name])
    registry = FakeRegistry(on_pypi)
    assert check("SLOP001", registry=registry).findings == []
    assert registry.queries == []


def test_options_and_paths_keep_a_file_a_pip_file(write, check):
    """The look-alike: every line of pip's own that starts like markup. An option, the
    folder itself (also with a marker or an option of its own), a path (also one
    into a hidden folder, with a backslash, or from the home folder), and a line
    that goes on from the one before. The invented name next to them is still
    reported."""
    lines = [
        "--pre",
        "-r other.txt",
        "-rother.txt",
        "-e .",
        "-e.",
        ".",
        "..",
        ".[dev]",
        "./local",
        "../up",
        ".wheels/a-1.0-py3-none-any.whl",
        ".\\local",
        "~/wheels/a-1.0-py3-none-any.whl",
        ". ; python_version >= '3.9'",
        ".; python_version >= '3.9'",
        ".. ; python_version >= '3.9'",
        ". --config-settings editable_mode=strict",
        "asgiref==3.7.2 \\",
        "    --hash=sha256:89b2ef2247e3b562a16eef663bc0e2e703ec6468e2fa8a5cd61cd449786d4f6e",
        "requests \\",
        "    >=2.0",
        "asgiref \\",
        "    ==3.7.2 \\",
        "    --hash=sha256:89b2ef2247e3b562a16eef663bc0e2e703ec6468e2fa8a5cd61cd449786d4f6e",
        "ghost-lib==1.0",
    ]
    write("requirements/base.txt", "\n".join(lines) + "\n")
    write("requirements/other.txt", "requests\n")
    registry = FakeRegistry({"asgiref", "requests"})
    result = check("SLOP001", registry=registry)
    assert found(result) == [("requirements/base.txt", len(lines), 1)]
    assert sorted(set(registry.queries)) == ["asgiref", "ghost-lib", "requests"]


def test_a_cmake_file_in_a_requirements_folder_is_not_read(write, check):
    """``tests/requirements/CMakeLists.txt`` is no pip file, and neither are its calls."""
    path = write(
        "tests/requirements/CMakeLists.txt",
        """
        cmake_minimum_required(VERSION 3.16)
        project(requirements_tests CXX)
        add_executable(requires_clause requires_clause.cpp)
        """,
    )
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert result.findings == [] and registry.queries == [] and result.files_checked == 0
    # Even when it is read as a requirements file, no line of it is a requirement.
    assert not any(dep.validate for dep in parse_dependency_file(path, path.parent).dependencies)


@pytest.mark.parametrize(
    "option", ["-f ./wheels", "--find-links=https://x.example/w", "--no-index"]
)
def test_find_links_makes_only_its_own_file_private(write, check, option):
    """A name missing from PyPI may come from --find-links. That covers the file the
    option is in, and no other file."""
    write("requirements.txt", f"{option}\nonly-in-links\n")
    write("setup.cfg", "[options]\ninstall_requires =\n    ghost-lib>=1\n")
    write("app.py", "import ghost_mod_zz\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, check_imports=True)
    assert found(result) == [("app.py", 1, 1), ("setup.cfg", 3, 1)]
    assert "only-in-links" not in registry.queries and result.notes == []


# --- Lists for another tool -------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        f"--pre --extra-index-url {PRIVATE}",
        f"--trusted-host pkgs.corp.example --index-url {PRIVATE}",
        f"--index-url https://pypi.org/simple --extra-index-url {PRIVATE}",
        f"--prefer-binary -i {PRIVATE}",
        f"--pre --extra-index-url={PRIVATE}  # the company index",
        f"-i{PRIVATE}",
        f"--extra-index {PRIVATE}",
        f"--i={PRIVATE}",
    ],
)
def test_an_index_option_counts_anywhere_on_an_option_line(write, check, line):
    """pip takes several options on one line, a value attached to ``-i``, and a long
    option cut short. The index is seen in each, for the file and for the files
    that inherit from it."""
    write("requirements.txt", f"{line}\ncorp-auth==1.0\n")
    write("setup.py", "from setuptools import setup\nsetup(install_requires=['corp-core'])\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert result.findings == [] and registry.queries == []
    assert "a private package index is set in requirements.txt" in result.notes[0]


@pytest.mark.parametrize(
    "line", ["--pre --find-links ./wheels", "-f./wheels", "--find ./wheels", "--pre --no-i"]
)
def test_a_links_option_counts_anywhere_on_an_option_line(write, check, line):
    """The same for ``--find-links`` and ``--no-index``, which cover their own file."""
    write("requirements.txt", f"{line}\ncorp-auth==1.0\n")
    write("constraints.txt", "ghost-pin==1.0\n")
    registry = FakeRegistry(set())
    assert found(check("SLOP001", registry=registry)) == [("constraints.txt", 1, 1)]
    assert registry.queries == ["ghost-pin"]


@pytest.mark.parametrize(
    "line",
    [
        "--pre --index-url https://pypi.org/simple",
        "--extra-index-url=https://pypi.org/simple --prefer-binary",
        "-ihttps://pypi.org/simple",
        # Not an index of the file: a hash that continues a requirement, PyPI's
        # address after a requirement, a comment, words of a sentence.
        "--hash=sha256:abc --hash=sha256:fff",
        "requests --index-url https://pypi.org/simple",
        "requests --hash=sha256:abc --global-option=--index-url",
        f"# --extra-index-url {PRIVATE}",
        f"this file is installed with --index-url {PRIVATE}",
        "--editable ./local --install-option=--fast",
    ],
)
def test_lines_that_name_no_private_index(write, check, line):
    """PyPI's own address, and words that only look like an index option."""
    write("requirements.txt", f"{line}\nghost-lib==1.0\n")
    registry = FakeRegistry({"requests"})
    result = check("SLOP001", registry=registry)
    assert found(result) == [("requirements.txt", 2, 1)] and result.notes == []


@pytest.mark.parametrize(
    "option",
    [
        f"--extra-index-url {PRIVATE}",
        f"--index-url={PRIVATE}",
        f"-i {PRIVATE}",
        "--extra-index-url https://pkgs.corp.example/simple",
        f"; python_version < '3.12' --index-url {PRIVATE}",
    ],
)
def test_an_index_option_after_a_requirement_makes_the_file_private(write, tmp_path, option):
    """``corp-auth==1.0 --extra-index-url URL``: pip takes no index from the end of a
    requirement line, but whoever wrote the line meant one. v0.2 kept such a name on
    the machine when the URL held a password. Now the file is private, for itself
    and for the files that inherit from it, also when one of them is checked alone."""
    write("requirements.txt", f"requests==2.32.0\ncorp-auth==1.0 {option}\n")
    setup = write(
        "setup.py", "from setuptools import setup\nsetup(install_requires=['corp-core'])\n"
    )
    for paths in ([tmp_path], [setup]):
        registry = FakeRegistry({"requests"})
        result = run(paths, tmp_path, registry, select=["SLOP001"])
        assert result.findings == [] and registry.queries == []
        assert "a private package index is set in requirements.txt" in result.notes[0]
        assert "secret" not in result.notes[0]


@pytest.mark.parametrize("option", ["--find-links https://u:p@files.corp.example/w", "--no-index"])
def test_a_links_option_after_a_requirement_covers_its_file(write, check, option):
    """The same for ``--find-links`` and ``--no-index``, which cover their own file
    and no other."""
    write("requirements.txt", f"requests==2.32.0\ncorp-auth==1.0 {option}\n")
    write("constraints.txt", "ghost-pin==1.0\n")
    registry = FakeRegistry({"requests"})
    assert found(check("SLOP001", registry=registry)) == [("constraints.txt", 1, 1)]
    assert registry.queries == ["ghost-pin"]


def test_a_url_in_an_option_after_a_requirement_keeps_the_name_on_the_machine(write, check):
    """v0.2 looked up no name whose line held a URL with a password, wherever the URL
    stood. An option after the requirement that is no index option still says where
    the package comes from: the name is not sent, from this file or another."""
    option = "--config-settings=url=https://deploy:secret@files.corp.example/x"
    write("requirements.txt", f"requests==2.32.0\ncorp-auth==1.0 {option}\n")
    write("constraints.txt", "corp-auth==1.0\nrequests==2.32.0\n")
    registry = FakeRegistry({"requests"})
    result = check("SLOP001", registry=registry)
    assert result.findings == [] and set(registry.queries) == {"requests"}
    # The look-alike: a hash is no URL, so the name is looked up, here and elsewhere.
    write("requirements.txt", "requests==2.32.0\ncorp-auth==1.0 --hash=sha256:abcd\n")
    registry = FakeRegistry({"requests"})
    result = check("SLOP001", registry=registry)
    assert found(result) == [("constraints.txt", 1, 1), ("requirements.txt", 2, 1)]


APT_LIST = "build-essential\nlibpq-dev\nlibxml2-dev\nrequests\n"


@pytest.mark.parametrize(
    "name", ["requirements/system.txt", "system-requirements.txt", "requirements/debian.in"]
)
def test_a_plain_list_that_pypi_mostly_lacks_is_not_reported(write, check, name):
    """One name per line and nothing else may be a list for apt or R. When PyPI lacks
    most of the names, the file is not a pip file, and nothing in it is reported."""
    write(name, APT_LIST)
    result = check("SLOP001", registry=FakeRegistry({"requests"}))
    assert result.findings == []


def test_a_plain_list_with_one_invented_name_is_reported(write, check):
    """A pip file without versions is a plain list too. PyPI has most of its names, so
    the one it lacks is reported."""
    write("requirements/base.in", "django\nrequests\ncelery\nghost-lib\n")
    write("requirements/half.in", "requests\nghost-half\n")
    result = check("SLOP001", registry=FakeRegistry({"django", "requests", "celery"}))
    assert found(result) == [("requirements/base.in", 4, 1), ("requirements/half.in", 2, 1)]


@pytest.mark.parametrize(
    "body",
    [
        "build-essential==12.9\nlibpq-dev\nlibxml2-dev\n",  # a version
        "-c constraints.txt\nlibpq-dev\nlibxml2-dev\n",  # an option
        "libpq-dev[extra]\nlibxml2-dev\n",  # an extra
        'libpq-dev ; python_version < "3.12"\nlibxml2-dev\n',  # a marker
        "libpq-dev \\\n    --hash=sha256:abc\nlibxml2-dev\n",  # a hash
        # A version in parentheses is pip syntax too, although Debian writes one the
        # same way. Only PyPI's answers could tell the two apart: a limit in the README.
        "libssl-dev (>= 1.1)\nlibpq-dev\nlibxml2-dev\n",
    ],
)
def test_a_file_with_pip_syntax_is_a_pip_file(write, check, body):
    """A version, an option, an extra, a marker or a hash is something only pip reads.
    Then every missing name is reported, however many there are."""
    write("requirements/system.txt", body)
    result = check("SLOP001", registry=FakeRegistry(set()))
    assert {"libpq-dev", "libxml2-dev"} <= {f.subject for f in result.findings}


BINDEP = (
    "# see https://docs.opendev.org/opendev/bindep/ for additional information.\n"
    "build-essential [platform:dpkg]\ngettext\nlibffi-dev [platform:dpkg]\n"
    "libffi-devel [platform:rpm]\n"
)


@pytest.mark.parametrize("name", ["other-requirements.txt", "requirements/bindep.txt"])
def test_a_bindep_list_is_not_a_pip_file(write, check, name):
    """bindep's ``[platform:dpkg]`` is no list of extras, and pip turns such a line
    down. So the line is no requirement and proves no pip file: nothing of the list
    is sent or reported, also when PyPI has one of its names."""
    write(name, BINDEP)
    for on_pypi in (set(), {"gettext"}):
        registry = FakeRegistry(on_pypi)
        assert check("SLOP001", registry=registry).findings == []
        assert registry.queries == []


def test_a_bracket_that_is_no_extra_leaves_a_plain_list_in_doubt(write, check):
    """One bindep line in a list of bare names does not make the list a pip file, as
    a real extra would. PyPI lacks every name, so nothing is reported."""
    write("requirements/system.txt", "build-essential\nlibpq-dev\nlibffi-dev [platform:dpkg]\n")
    assert check("SLOP001", registry=FakeRegistry(set())).findings == []


def test_requirements_txt_is_never_doubted(write, check):
    """The files v0.2 read keep their behaviour: a plain ``requirements.txt`` whose
    names PyPI lacks is reported in full."""
    write("requirements.txt", "ghost-one\nghost-two\n")
    write("requirements-dev.txt", "ghost-three\n")
    result = check("SLOP001", registry=FakeRegistry(set()))
    assert [f.subject for f in result.findings] == ["ghost-three", "ghost-one", "ghost-two"]


def test_no_answer_never_makes_a_plain_list_a_pip_file(write, check):
    """Names PyPI gave no answer for don't count as found. So a list of apt packages
    stays silent when half the lookups fail."""

    class Half(FakeRegistry):
        """Answers for names that start with "lib", and for no other."""

        def exists(self, name):
            """Unknown unless the name starts with "lib"."""
            return super().exists(name) if name.startswith("lib") else None

    write("requirements/system.txt", APT_LIST)
    assert check("SLOP001", registry=Half(set())).findings == []


def test_a_plain_list_is_not_reported_while_answers_are_missing(write, check):
    """PyPI answers two names and gives no answer for the rest (a rate limit, a
    dropped connection). The names it did not answer may all be missing, so the
    file may still be a list for another tool: nothing is reported. With every
    answer in, the same file is silent too."""

    class Partial(FakeRegistry):
        """Knows ``curl``, lacks ``build-essential``, and can't say for any other."""

        def exists(self, name):
            """An answer for the two names, unknown for the rest."""
            return super().exists(name) if name in ("curl", "build-essential") else None

    names = "curl\nbuild-essential\nlibxml2-dev\nlibffi-dev\nlibssl-dev\npython3-dev\n"
    write("requirements/system.txt", names)
    assert check("SLOP001", registry=Partial({"curl"})).findings == []
    assert check("SLOP001", registry=FakeRegistry({"curl"})).findings == []


# --- setup.cfg --------------------------------------------------------------------------

SETUP_CFG = """
[metadata]
name = demo
description = requests

[options]
packages = find:
install_requires =
    requests>=2
    # a comment line
    ghost-lib ; python_version < "3.11"
    corp-lib @ https://example.com/corp.whl
    ignored-lib  # slopfence: ignore
setup_requires = pbr; setuptools
tests_require = pytest; python_version > "3"
python_requires = >=3.8

[options.extras_require]
dev =
    ghost-dev
    demo[docs]
docs = sphinx
all = file: requirements-all.txt

[options.entry_points]
console_scripts =
    demo = demo.cli:main

[flake8]
ignore = E501
"""


def test_setup_cfg_found_and_missing(write, check, tmp_path):
    """install_requires, setup_requires, tests_require and extras are read, each on its line."""
    path = write("setup.cfg", SETUP_CFG)
    assert brief(path, tmp_path) == [
        ("requests", 8, True),
        ("ghost-lib", 10, True),
        ("corp-lib", 11, False),
        ("ignored-lib", 12, True),
        ("pbr", 13, True),
        ("setuptools", 13, True),
        ("pytest", 14, True),
        ("ghost-dev", 19, True),
        ("demo", 20, True),
        ("sphinx", 21, True),
    ]
    assert parse_dependency_file(path, tmp_path).project_names == {"demo"}
    registry = FakeRegistry({"requests", "pbr", "setuptools", "pytest", "sphinx"})
    result = check("SLOP001", registry=registry)
    assert found(result) == [("setup.cfg", 10, 1), ("setup.cfg", 19, 1)]
    # A direct reference, an ignored line and the project's own name are never sent.
    assert not {"corp-lib", "ignored-lib", "demo"} & set(registry.queries)


@pytest.mark.parametrize(
    "body",
    [
        "[options]\ninstall_requires = file: requirements.txt\n",
        "no section header\n",
        "[options]\ninstall_requires =\n    a\n\n[options]\nx = 1\n",  # duplicate section
        "[options]\ninstall_requires = a\ninstall_requires = b\n",  # duplicate key
        "[flake8]\nmax-line-length = 160\nignore = W503,E402\n",
        "[options]\ninstall_requires =\n    %(name)s-core\n",
        "[options]\npython_requires = >=3.8\nzip_safe = False\n",
        "[options]\ninstall_requires = requests, flask\n",  # commas: setuptools rejects it
        "[options]\n= no option name\n",
    ],
)
def test_setup_cfg_without_readable_dependencies(write, tmp_path, body):
    """file: directives, broken files and tool-only files give nothing, silently."""
    assert parse_dependency_file(write("setup.cfg", body), tmp_path).dependencies == []


# Lines an INI file is made of, and ways to indent them: enough to build files that
# ``configparser`` reads in every way it can.
CFG_LINES = [
    "[options]",
    "[metadata]",
    "[options.extras_require]",
    "[a]b]",
    "[x] # c",
    "install_requires =",
    "install_requires = a",
    "name = demo",
    "key: value",
    "k = v ; w",
    "dev =",
    "requests>=2",
    "# comment",
    "; comment",
    "",
    "   ",
    "= novalue",
    "a = b = c",
    "x\x0cy",
    "x\ry",
    "\x0c",
    "x\u2028y",
    "K = v",
]
CFG_INDENTS = ["", "", " ", "    ", "\t", " \t", "\x0c  "]


def test_setup_cfg_lines_are_the_lines_configparser_reads():
    """Line numbers come from slopfence's own reading of the file. It must find the
    value lines that ``configparser`` finds, on every Python version, and each on the
    line it names. Here that is checked for 3,000 small files."""
    rng = random.Random(53)
    compared = 0
    for _ in range(3000):
        text = "\n".join(
            rng.choice(CFG_INDENTS) + rng.choice(CFG_LINES) for _ in range(rng.randint(1, 8))
        )
        parser = configparser.RawConfigParser()
        parser.optionxform = str
        try:
            parser.read_string(text)
        except (configparser.Error, ValueError):
            continue
        expected = {
            (section, option): lines
            for section in parser.sections()
            for option, value in parser.items(section, raw=True)
            if (lines := [v.strip() for v in value.split("\n") if v.strip()])
        }
        scanned: dict[tuple[str, str], list[str]] = {}
        for section, option, (lineno, value, _) in _cfg_lines(text):
            scanned.setdefault((section, option), []).append(value)
            assert value in text.split("\n")[lineno - 1], (text, lineno)
        assert scanned == expected, text
        compared += 1
    assert compared > 200  # most random files are not INI files; enough are


def test_a_form_feed_does_not_shift_setup_cfg_lines(tmp_path, check):
    """Old files use a form feed as a page break. It is not a line break: the finding
    is on the line that git and every editor count."""
    (tmp_path / "setup.cfg").write_text(
        "[metadata]\nname = demo\n\x0c\n[options]\ninstall_requires =\n    ghost-lib\x0c\n",
        encoding="utf-8",
    )
    assert found(check("SLOP001", registry=FakeRegistry(set()))) == [("setup.cfg", 6, 1)]


def test_text_lines_end_at_line_feeds_only():
    """A form feed, U+0085 and U+2028 are characters of their line, and a carriage
    return before a line feed is part of the line end. An ordinary text has the
    lines that ``str.splitlines`` gives, so no line number of such a file moves."""
    assert text_lines("a\nb\r\nc\x0cd\x85e\u2028f\n") == ["a", "b", "c\x0cd\x85e\u2028f", ""]
    ordinary = "requests==2.31\n\n# pins\r\n  flask \\\n    --hash=sha256:abc\n"
    assert text_lines(ordinary) == [*ordinary.splitlines(), ""]


# Characters that ``str.splitlines`` ends a line at, and git and editors don't.
NOT_LINE_ENDS = ["\x0c", "\x0b", "\x1c", "\x85", "\u2028", "\u2029"]


@pytest.mark.parametrize("char", NOT_LINE_ENDS)
@pytest.mark.parametrize("name", ["constraints.txt", "requirements.txt"])
def test_a_form_feed_or_line_separator_does_not_shift_requirements_lines(
    tmp_path, check, name, char
):
    """The finding of a requirements file is on the line that git counts, whatever
    characters the lines above it hold."""
    text = f"# pins{char}\nrequests==2.31\nghost-pin==1.0\n"
    (tmp_path / name).write_text(text, encoding="utf-8")
    assert found(check("SLOP001", registry=FakeRegistry({"requests"}))) == [(name, 3, 1)]


# What may stand in front of such a character, on the same line of the file: a
# comment, a path, a sentence.
BEFORE_A_BREAK = ["# internal packages", "./vendor/pkg", "Internal packages below"]
SOURCE_OPTIONS = [f"--index-url {PRIVATE}", f"--extra-index-url={PRIVATE}", "--find-links ./wheels"]


@pytest.mark.parametrize("char", NOT_LINE_ENDS)
@pytest.mark.parametrize("before", BEFORE_A_BREAK)
@pytest.mark.parametrize("option", SOURCE_OPTIONS)
def test_an_option_after_a_line_break_that_only_pip_sees_still_counts(
    tmp_path, check, char, before, option
):
    """pip ends a line at a form feed, U+0085 or U+2028 too. So for pip the option
    after such a character is a line of its own, and the file's packages come from
    there: none of them is sent, as in v0.2. The line numbers stay the ones of git."""
    text = f"requests==2.31\n{before}{char}{option}\ncorp-auth==1.0\n"
    (tmp_path / "requirements.txt").write_text(text, encoding="utf-8")
    registry = FakeRegistry({"requests"})
    assert check("SLOP001", registry=registry).findings == [] and registry.queries == []

    (tmp_path / "requirements.txt").write_text(text.replace(option, "--pre"), encoding="utf-8")
    registry = FakeRegistry({"requests"})
    assert found(check("SLOP001", registry=registry)) == [("requirements.txt", 3, 1)]


# In a file that is not UTF-8 nothing turns a lone carriage return into a line feed.
ENCODED_BREAKS = [
    *((encoding, char) for encoding in ("utf-8", "utf-16") for char in NOT_LINE_ENDS),
    ("latin-1", "\x0c"),
    ("latin-1", "\r"),
]


@pytest.mark.parametrize(("encoding", "char"), ENCODED_BREAKS)
def test_an_index_after_such_a_line_break_covers_the_files_that_inherit(
    tmp_path, check, encoding, char
):
    """The same for the files that inherit a private index, also from a requirements
    file that is not UTF-8. There a lone carriage return is such a line break too."""
    text = f"# caf\u00e9{char}--index-url {PRIVATE}{char}corp-auth==1.0\n"
    (tmp_path / "requirements.txt").write_bytes(text.encode(encoding))
    (tmp_path / "setup.cfg").write_text("[options]\ninstall_requires =\n    corp-core\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert result.findings == [] and registry.queries == []
    assert "a private package index is set in requirements.txt" in result.notes[0]


@pytest.mark.parametrize("char", NOT_LINE_ENDS)
@pytest.mark.parametrize("entry", [f"corp-auth @ {GIT}", f"-e {GIT}#egg=corp-auth"])
def test_a_sourced_entry_after_such_a_line_break_still_counts(tmp_path, check, char, entry):
    """And for an entry with a source of its own: it is a line for pip, so the package
    is not looked up in the file that names it again."""
    (tmp_path / "requirements.txt").write_text(f"# vcs{char}{entry}\n", encoding="utf-8")
    (tmp_path / "setup.cfg").write_text("[options]\ninstall_requires =\n    corp-auth\n")
    registry = FakeRegistry(set())
    assert check("SLOP001", registry=registry).findings == [] and registry.queries == []


@pytest.mark.parametrize("char", NOT_LINE_ENDS)
def test_an_ignore_comment_covers_all_that_pip_reads_on_its_line(tmp_path, check, char):
    """Two requirements on one line of the file, for pip two lines: each is reported
    on the line that git counts, and an ignore comment there covers both."""
    path = tmp_path / "requirements.txt"
    path.write_text(f"ghost-one{char}ghost-two\nghost-three\n", encoding="utf-8")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert [(f.subject, f.line) for f in result.findings] == [
        ("ghost-one", 1),
        ("ghost-two", 1),
        ("ghost-three", 2),
    ]
    text = f"ghost-one{char}ghost-two  # slopfence: ignore\nghost-three\n"
    path.write_text(text, encoding="utf-8")
    registry = FakeRegistry(set())
    assert found(check("SLOP001", registry=registry)) == [("requirements.txt", 2, 1)]
    assert registry.queries == ["ghost-three"]


# The same list in the two TOML files, with a comment line above it that holds {char}.
TOML_LISTS = {
    "Pipfile": (
        '[packages]\n# from the wiki{char}keep sorted\nrequests = "*"\nghost-lib = "*"\n'
        'ghost-quiet = "*"  # slopfence: ignore\n',
        4,
    ),
    "pyproject.toml": (
        '[project]\nname = "demo"\n# from the wiki{char}keep sorted\ndependencies = [\n'
        '    "requests",\n    "ghost-lib",\n    "ghost-quiet",  # slopfence: ignore\n]\n',
        6,
    ),
}


# A form feed is not here: TOML takes none, so such a file declares nothing.
@pytest.mark.parametrize("char", ["\x85", "\u2028", "\u2029"])
@pytest.mark.parametrize("name", sorted(TOML_LISTS))
def test_a_line_separator_does_not_move_a_toml_ignore_comment(tmp_path, check, name, char):
    """In a ``Pipfile`` or ``pyproject.toml`` the entry and its ignore comment are
    found on the same line. Before, a line separator above them moved the comment to
    the entry before: the ignored name was sent, and the other one never was."""
    text, line = TOML_LISTS[name]
    (tmp_path / name).write_text(text.format(char=char), encoding="utf-8")
    registry = FakeRegistry({"requests"})
    assert found(check("SLOP001", registry=registry)) == [(name, line, 1)]
    assert sorted(registry.queries) == ["ghost-lib", "requests"]


def test_setup_cfg_details(write, tmp_path):
    """Dashed keys, tabs, ignore-file and dependency_links."""
    path = write("setup.cfg", "[options]\nInstall-Requires =\n\tghost\n")
    assert brief(path, tmp_path) == [("ghost", 3, True)]
    path = write("setup.cfg", "# slopfence: ignore-file\n[options]\ninstall_requires = ghost\n")
    assert [d.suppressed for d in parse_dependency_file(path, tmp_path).dependencies] == [True]
    path = write(
        "setup.cfg",
        "[options]\ndependency_links = https://corp.example/simple\ninstall_requires = corp-a\n",
    )
    assert brief(path, tmp_path) == [("corp-a", 3, False)]
    assert parse_dependency_file(path, tmp_path).private_index


@pytest.mark.parametrize(
    ("value", "names"),
    [
        ('a; python_version < "3"', ["a"]),
        ('a; python_version < "3"; b', ["a", "b"]),
        # Without quotes the marker would read as a package called python_version.
        ("a; python_version < 3", ["a"]),
        ("a; sys_platform == win32; b", ["a", "b"]),
        ("pbr; setuptools", ["pbr", "setuptools"]),
    ],
)
def test_setup_cfg_one_line_lists_are_cut_at_semicolons(write, tmp_path, value, names):
    """A value on the key's line is cut at ``;``, as setuptools does. A part that is a
    marker is not a package, and the parts after it still count."""
    path = write("setup.cfg", f"[options]\ntests_require = {value}\n")
    assert [d.name for d in parse_dependency_file(path, tmp_path).dependencies] == names


def test_setup_cfg_marker_on_a_line_of_a_list_is_one_requirement(write, tmp_path):
    """In a list with one requirement per line, ``;`` starts that line's marker."""
    path = write("setup.cfg", '[options]\ninstall_requires =\n    a; python_version < "3"\n    b\n')
    assert brief(path, tmp_path) == [("a", 3, True), ("b", 4, True)]
    # A package may be called like a marker variable. Only a part after a ";" is doubted.
    path = write("setup.cfg", "[options]\ninstall_requires =\n    extra\n    requests\n")
    assert brief(path, tmp_path) == [("extra", 3, True), ("requests", 4, True)]
    path = write("setup.cfg", "[options]\ninstall_requires = extra; requests\n")
    assert brief(path, tmp_path) == [("extra", 2, True), ("requests", 2, True)]


# --- setup.py ---------------------------------------------------------------------------

SETUP_PY = """
from setuptools import setup

setup(
    name="demo",
    install_requires=[
        "requests>=2",
        "ghost-lib",
        "ghost-ignored",  # slopfence: ignore[SLOP001]
        "corp @ git+https://example.com/corp.git",
    ],
    extras_require={"dev": ["pytest", "demo[docs]"], ":python_version<'3.8'": ["ghost-extra"]},
    setup_requires="pbr",
    tests_require=("nose",),
    python_requires=">=3.8",
    packages=["demo", "demo.sub"],
    entry_points={"console_scripts": ["demo = demo.cli:main"]},
)
"""


def test_setup_py_found_and_missing(write, check, tmp_path):
    """Literal lists, strings, tuples and dicts passed to setup() are read."""
    path = write("setup.py", SETUP_PY)
    declared = parse_dependency_file(path, tmp_path)
    assert [(d.name, d.line, d.col, d.validate, d.suppressed) for d in declared.dependencies] == [
        ("requests", 6, 9, True, False),
        ("ghost-lib", 7, 9, True, False),
        ("ghost-ignored", 8, 9, True, True),
        ("corp", 9, 9, False, False),
        ("pytest", 11, 29, True, False),
        ("demo", 11, 39, True, False),
        ("ghost-extra", 11, 80, True, False),
        ("pbr", 12, 20, True, False),
        ("nose", 13, 20, True, False),
    ]
    assert declared.project_names == {"demo"}
    registry = FakeRegistry({"requests", "pytest", "pbr", "nose"})
    result = check("SLOP001", registry=registry)
    assert found(result) == [("setup.py", 7, 9), ("setup.py", 11, 80)]
    assert [f.subject for f in result.findings] == ["ghost-lib", "ghost-extra"]
    assert result.files_checked == 1  # a Python file and a dependency file, counted once
    # An ignored line, a git reference and the project's own name are never sent.
    assert not {"ghost-ignored", "corp", "demo"} & set(registry.queries)


def test_setup_py_follows_names(write, tmp_path):
    """Variables assigned literals, list concatenation and **kwargs are followed."""
    path = write(
        "setup.py",
        """
        import sys
        import setuptools

        VERSION = "1.0"
        requires = ["requests", "six"]
        if sys.platform == "win32":
            requires += ["pywin32"]
        extras = {"docs": ["sphinx"]}
        extras["all"] = extras["docs"] + ["ghost-extra"]
        test_deps: list[str] = ["pytest", "pytest-cov"]
        numpy_req = "numpy>=1.20"
        kwargs = dict(tests_require=test_deps)
        kwargs["setup_requires"] = ["wheel"] if sys.version_info > (3,) else ["wheel", "six"]
        more = {"extras_require": {**extras, "x": [*test_deps, "ghost-x"]}}

        setuptools.setup(
            name="demo",
            version=VERSION,
            install_requires=requires + [numpy_req, "demo-core==" + VERSION, f"x=={VERSION}"],
            **kwargs,
            **more,
        )
        """,
    )
    declared = parse_dependency_file(path, tmp_path).dependencies
    assert [(d.name, d.line) for d in declared] == [
        ("requests", 5),
        ("six", 5),
        ("pywin32", 7),
        ("sphinx", 8),
        ("ghost-extra", 9),
        ("pytest", 10),
        ("pytest-cov", 10),
        ("numpy", 11),
        ("wheel", 13),
        ("wheel", 13),
        ("six", 13),
        ("ghost-x", 14),
    ]


def test_setup_py_skips_everything_computed(write, tmp_path, check):
    """Calls, comprehensions and built strings are never guessed at, and never run."""
    marker = tmp_path / "executed"
    path = write(
        "setup.py",
        f"""
        from setuptools import setup

        open({str(marker)!r}, "w").close()

        def read(name):
            return open(name).read().splitlines()

        version = "1.0"
        prefix = "demo-"
        setup(
            name="demo",
            install_requires=read("requirements.txt"),
            extras_require={{"dev": [line for line in read("dev.txt")], "x": [prefix + "plugin"]}},
            tests_require=sorted({{"a", "b"}}),
            setup_requires=["pkg==" + version, "pkg-%s" % version, "{{}}-x".format(prefix)],
        )
        """,
    )
    assert parse_dependency_file(path, tmp_path).dependencies == []
    registry = FakeRegistry(set())
    assert check("SLOP001", registry=registry).findings == []
    assert registry.queries == [] and not marker.exists()


@pytest.mark.parametrize(
    ("body", "names"),
    [
        # The name first holds a file name, then the list that was read from it.
        (
            'reqs = "requirements.txt"\nwith open(reqs) as f:\n    reqs = f.read().splitlines()\n'
            "setup(install_requires=reqs)\n",
            [],
        ),
        ('reqs = "base.txt"\nreqs = load(reqs)\nsetup(install_requires=reqs)\n', []),
        # A string that is added to is being built: no part of it is a requirement.
        ('req = "demo"\nreq += suffix\nsetup(install_requires=[req])\n', []),
        ('req = "plugin"\nreq += "-core"\nsetup(install_requires=req)\n', []),
        # A name that only ever holds a string is that string.
        ('req = "numpy>=1.20"\nsetup(install_requires=[req, "six"])\n', ["numpy", "six"]),
        ('req = "a" if PY3 else "b"\nsetup(setup_requires=req)\n', ["a", "b"]),
        # Another function's variable of the same name doesn't hide the list.
        (
            'def version():\n    reqs = "__version__"\n    return find(reqs)\n\n'
            'reqs = ["requests"]\nsetup(install_requires=reqs)\n',
            ["requests"],
        ),
        # A space-separated string is not a requirement.
        ('setup(install_requires="requests flask")\n', []),
        # Extras that a function returns are not known.
        ("setup(extras_require=load_extras())\n", []),
    ],
)
def test_setup_py_names_that_hold_strings(write, tmp_path, body, names):
    """A string assigned to a name is a requirement only when the name never holds
    anything else. ``reqs = "requirements.txt"`` is a file name."""
    path = write("setup.py", "from setuptools import setup\n" + body)
    assert [d.name for d in parse_dependency_file(path, tmp_path).dependencies] == names


@pytest.mark.parametrize(
    ("body", "names"),
    [
        # The name was first used for something else: folders for package_data.
        (
            'extras = {"data": ["schemas", "templates"]}\npackage_data = {"demo": extras["data"]}\n'
            'extras = {"docs": ["sphinx"]}\n'
            "setup(package_data=package_data, extras_require=extras)\n",
            ["sphinx"],
        ),
        (
            'reqs = ["old"]\nreqs += ["older"]\nreqs = ["new"]\nsetup(install_requires=reqs)\n',
            ["new"],
        ),
        (
            'extras = {}\nextras["dev"] = ["old"]\nextras = {"docs": ["new"]}\n'
            "setup(extras_require=extras)\n",
            ["new"],
        ),
        # A new value that uses the old one adds to it.
        ('reqs = ["a"]\nreqs = reqs + ["b"]\nsetup(install_requires=reqs)\n', ["a", "b"]),
        ('reqs = ["a"]\nreqs = sorted(reqs)\nsetup(install_requires=reqs)\n', ["a"]),
        # An assignment that may not run replaces nothing.
        ('reqs = ["a"]\nif WINDOWS:\n    reqs = ["b"]\nsetup(install_requires=reqs)\n', ["a", "b"]),
        (
            'try:\n    reqs = read()\nexcept OSError:\n    reqs = ["a"]\n'
            "setup(install_requires=reqs)\n",
            ["a"],
        ),
        (
            'reqs = ["a"]\n\ndef main():\n    reqs = ["b"]\n    setup(install_requires=reqs)\n',
            ["a", "b"],
        ),
    ],
)
def test_setup_py_a_new_value_replaces_the_old_one(write, tmp_path, body, names):
    """A plain assignment at the top of the file replaces what the name held before.
    What was assigned above it was for something else and is not a requirement."""
    path = write("setup.py", "from setuptools import setup\n" + body)
    assert [d.name for d in parse_dependency_file(path, tmp_path).dependencies] == names


def test_setup_py_details(write, tmp_path):
    """Multi-line strings, dependency_links, ignore-file and broken files."""
    path = write(
        "setup.py",
        '''
        from setuptools import setup

        setup(
            install_requires="""
                requests>=2
                ghost-lib   # a comment
            """,
            dependency_links=["https://corp.example/simple"],
        )
        ''',
    )
    assert brief(path, tmp_path) == [("requests", 5, False), ("ghost-lib", 6, False)]
    assert parse_dependency_file(path, tmp_path).private_index
    path = write(
        "setup.py",
        "# slopfence: ignore-file\nfrom setuptools import setup\nsetup(install_requires=['x'])\n",
    )
    assert [d.suppressed for d in parse_dependency_file(path, tmp_path).dependencies] == [True]
    assert parse_dependency_file(write("setup.py", "def broken(:\n"), tmp_path).dependencies == []


@pytest.mark.parametrize(
    "body",
    [
        # A Sphinx extension.
        "def setup(app):\n    app.add_config_value('x', 1, 'env')\n",
        # A task runner whose task is called setup, and takes the same keyword.
        "def setup(install_requires=('git',)):\n    pass\n\nsetup(install_requires=['nginx'])\n",
        # Keywords of another call.
        "from x import cythonize\ncythonize('a.pyx', install_requires=['not-a-kwarg'])\n",
        "from numpy.distutils.core import setup\nsetup(**configuration(top_path='').todict())\n",
    ],
)
def test_setup_calls_that_are_not_packaging(write, tmp_path, body):
    """A ``setup`` the file defines itself, and other calls, declare nothing."""
    assert parse_dependency_file(write("setup.py", body), tmp_path).dependencies == []


def test_a_method_called_setup_does_not_hide_the_real_call(write, tmp_path):
    """Only a ``setup`` defined at the top of the file replaces setuptools' one."""
    path = write(
        "setup.py",
        "from setuptools import setup\n\nclass Build:\n    def setup(self):\n        pass\n\n"
        "setup(install_requires=['requests'])\n",
    )
    assert brief(path, tmp_path) == [("requests", 7, True)]


def test_setup_py_more_ways_to_pass_the_lists(write, tmp_path):
    """``dict(...)`` for extras, ``**`` of a dict literal, a variable for the project's
    name, and assignments the parser has no use for."""
    path = write(
        "setup.py",
        """
        import os
        from setuptools import setup

        NAME = "acme-core"
        here, _ = os.path.split(__file__)
        os.environ["X"] = "1"
        base = {"install_requires": ["requests"]}
        setup(
            name=NAME,
            extras_require=dict(dev=["pytest"], **{"docs": ["sphinx"]}),
            **{**base, "tests_require": ["nose"]},
        )
        """,
    )
    declared = parse_dependency_file(path, tmp_path)
    assert [d.name for d in declared.dependencies] == ["requests", "pytest", "nose"]
    assert declared.project_names == {"acme-core"}


def test_setup_py_long_and_deep_expressions(write, tmp_path):
    """A sum of many lists is read in full. Deeply nested expressions are read as far
    as twenty levels and never end the run."""
    many = " + ".join(f'["pkg{n}"]' for n in range(300))
    path = write("setup.py", f"from setuptools import setup\nsetup(install_requires={many})\n")
    assert len(parse_dependency_file(path, tmp_path).dependencies) == 300

    chain = " if x else ".join(f'"alt{n}"' for n in range(150))
    nested_list = "[*" * 150 + '["deep"]' + "]" * 150
    nested_dict = "{**" * 150 + '{"dev": ["deep-extra"]}' + "}" * 150
    nested_kwargs = "{**" * 150 + '{"tests_require": ["deep-test"]}' + "}" * 150
    path = write(
        "setup.py",
        "from setuptools import setup\n"
        f"setup(install_requires={chain}, setup_requires={nested_list},\n"
        f"      extras_require={nested_dict}, **{nested_kwargs})\n",
    )
    names = [d.name for d in parse_dependency_file(path, tmp_path).dependencies]
    assert names == [f"alt{n}" for n in range(20)]


def repeated(levels):
    """A setup.py where each list is the one before it, eight times over."""
    lists = "".join(f"a{n} = " + " + ".join([f"a{n - 1}"] * 8) + "\n" for n in range(1, levels))
    return (
        "from setuptools import setup\na0 = ['requests']\n"
        f"{lists}setup(name='demo', install_requires=a{levels - 1})\n"
    )


def test_a_name_used_many_times_is_not_followed_without_end(write, tmp_path):
    """A name is followed each time it is used. Eight uses of the list before it, seven
    lists deep, would be more than two million steps for a file of 400 bytes. A file
    gets a fixed number of steps. One that needs more is generated, and declares
    nothing that can be relied on."""
    path = write("setup.py", repeated(levels=3))
    assert brief(path, tmp_path) == [("requests", 2, True)]
    path = write("setup.py", repeated(levels=8))
    start = time.perf_counter()
    declared = parse_dependency_file(path, tmp_path)
    assert time.perf_counter() - start < 5
    assert declared.dependencies == [] and declared.project_names == {"demo"}


@pytest.mark.parametrize(
    "expression",
    ["~" * 20_000 + "1", "lambda: " * 5000 + "1"],
    ids=["20,000 unary operators", "5,000 nested lambdas"],
)
def test_a_setup_py_too_deep_for_the_parser_does_not_stop_the_run(write, tmp_path, expression):
    """Every setup.py of the project is read for the name it gives the project, also
    one that is not checked. One that Python's parser gives up on declares nothing.
    With every rule on, the rest of the project is read for duplicates too: the file
    never ends the run. Where it is checked itself, it is one parse error."""
    deep = write("vendor/generated/setup.py", f"from setuptools import setup\nx = {expression}\n")
    checked = write("requirements.txt", "ghost-lib\n")
    assert parse_dependency_file(deep, tmp_path).dependencies == []
    for rules in (["SLOP001"], None):
        result = run([checked], tmp_path, FakeRegistry(set()), select=rules)
        assert found(result) == [("requirements.txt", 1, 1)] and result.parse_errors == []
        result = run([tmp_path], tmp_path, FakeRegistry(set()), select=rules)
        assert found(result) == [("requirements.txt", 1, 1)]
        assert result.parse_errors == ["vendor/generated/setup.py"]


@needs_parser_depth_limit
def test_a_setup_py_with_a_sum_of_100_000_strings_does_not_stop_the_run(write, tmp_path):
    """Whether Python's parser takes a sum this long depends on its version. Where it
    gives up, the file is one parse error. Where it returns a tree 100,000 levels
    deep, the file is read like any other. Either way SLOP001 goes on and the setup.py
    declares no dependency. (Only SLOP001 runs here: the code-quality rules have a
    depth limit of their own on such a tree, which is issue #66.)"""
    text = "from setuptools import setup\nx = " + '"a" + ' * 100_000 + '"b"\n'
    deep = write("vendor/generated/setup.py", text)
    write("requirements.txt", "ghost-lib\n")
    assert parse_dependency_file(deep, tmp_path).dependencies == []
    result = run([tmp_path], tmp_path, FakeRegistry(set()), select=["SLOP001"])
    assert found(result) == [("requirements.txt", 1, 1)]
    assert result.parse_errors in ([], ["vendor/generated/setup.py"])


def test_setup_py_is_still_checked_as_python(write, check):
    """Other rules keep running on setup.py, and a broken one is one parse error."""
    write("setup.py", "# I hope this helps!\nfrom setuptools import setup\nsetup()\n")
    assert [f.rule for f in check().findings] == ["SLOP051"]
    write("setup.py", "def broken(:\n")
    result = check("SLOP001", registry=FakeRegistry(set()))
    assert result.parse_errors == ["setup.py"] and result.findings == []


def test_imports_in_setup_py_are_still_looked_up(write, tmp_path, not_installed):
    """``setup.py`` is a dependency file and a Python file. With ``check_imports`` its
    imports are looked up like those of any other file, as in v0.2."""
    path = write("setup.py", "import ghost_build_helper\nfrom setuptools import setup\n")
    write("app.py", "import ghost_app_helper\n")
    for paths in ([tmp_path], [path]):
        registry = FakeRegistry({"setuptools"})
        result = run(paths, tmp_path, registry, select=["SLOP001"], check_imports=True)
        assert ("setup.py", "ghost_build_helper") in [(f.path, f.subject) for f in result.findings]
        assert "ghost_build_helper" in registry.queries


# --- Pipfile ----------------------------------------------------------------------------

PIPFILE = """
[[source]]
url = "https://pypi.org/simple"
verify_ssl = true
name = "pypi"

[packages]
requests = "*"
ghost-lib = ">=1.0"
ghost-ignored = "*"   # slopfence: ignore
flask = {version = "*", extras = ["async"], index = "pypi"}
corp-git = {git = "https://example.com/corp.git", ref = "main"}
local = {path = ".", editable = true}
wheel-url = {file = "https://example.com/x.whl"}
odd-index = {version = "*", index = "corp"}
"zope.interface" = "*"
odd-shape = {version = "*", something_new = true}

[dev-packages]
ghost-dev = "*"

[requires]
python_version = "3.12"
"""


def test_pipfile_found_and_missing(write, check, tmp_path):
    """[packages] and [dev-packages] are checked; git, path, file and odd indexes aren't."""
    path = write("Pipfile", PIPFILE)
    declared = parse_dependency_file(path, tmp_path).dependencies
    assert [(d.name, d.line, d.validate, d.suppressed) for d in declared] == [
        ("requests", 7, True, False),
        ("ghost-lib", 8, True, False),
        ("ghost-ignored", 9, True, True),
        ("flask", 10, True, False),
        ("corp-git", 11, False, False),
        ("local", 12, False, False),
        ("wheel-url", 13, False, False),
        ("odd-index", 14, False, False),
        ("zope.interface", 15, True, False),
        ("odd-shape", 16, False, False),
        ("ghost-dev", 19, True, False),
    ]
    registry = FakeRegistry({"requests", "flask", "zope.interface"})
    assert found(check("SLOP001", registry=registry)) == [("Pipfile", 8, 1), ("Pipfile", 19, 1)]
    assert set(registry.queries) == {
        "requests",
        "ghost-lib",
        "flask",
        "zope.interface",
        "ghost-dev",
    }


def test_pipfile_private_source_and_details(write, check, tmp_path):
    """A [[source]] that isn't PyPI makes the file's packages private."""
    write("Pipfile", f'[[source]]\nname = "corp"\nurl = "{PRIVATE}"\n\n[packages]\ncorp-a = "*"\n')
    registry = FakeRegistry(set())
    assert check("SLOP001", registry=registry).findings == []
    assert registry.queries == []
    path = write("Pipfile", '[packages]\na = {version = "*", index = "pypi"}\n')
    assert brief(path, tmp_path) == [("a", 2, True)]
    path = write("Pipfile", "# slopfence: ignore-file\n[packages]\nghost = '*'\n")
    assert [d.suppressed for d in parse_dependency_file(path, tmp_path).dependencies] == [True]
    assert parse_dependency_file(write("Pipfile", "[packages\n"), tmp_path).dependencies == []
    assert parse_dependency_file(write("Pipfile", "packages = 3\n"), tmp_path).dependencies == []


PIPFILE_TABLE = '[packages]  # slopfence: ignore\nrequests = "*"\n\n'
POETRY_HEAD = '[tool.poetry]\nname = "demo"\n\n'
POETRY_TABLE = POETRY_HEAD + '[tool.poetry.dependencies]\npython = "^3.10"\nrequests = "*"\n\n'
# An entry written as a table of its own: (file, what stands above it, its header).
OWN_TABLES = [
    ("Pipfile", PIPFILE_TABLE, "[packages.ghost-lib]"),
    ("Pipfile", PIPFILE_TABLE, "[dev-packages.ghost-lib]"),
    ("Pipfile", PIPFILE_TABLE, '[packages."ghost-lib"]'),
    ("Pipfile", PIPFILE_TABLE, "[ packages . 'ghost-lib' ]"),
    (
        "Pipfile",
        '[[source]]\nname = "pypi"\nurl = "https://pypi.org/simple"\n\n',
        "[packages.ghost-lib]",
    ),
    ("pyproject.toml", POETRY_TABLE, "[tool.poetry.dependencies.ghost-lib]"),
    ("pyproject.toml", POETRY_TABLE, "[tool.poetry.group.dev.dependencies.ghost-lib]"),
]


@pytest.mark.parametrize(
    ("name", "above", "header"),
    OWN_TABLES,
    ids=[
        "packages",
        "dev-packages",
        "double quotes",
        "spaces and single quotes",
        "no packages table",
        "poetry",
        "poetry group",
    ],
)
def test_an_entry_that_is_a_table_of_its_own_is_on_its_header_line(
    write, check, name, above, header
):
    """``[packages.ghost-lib]`` is reported on that line, not on the line of
    ``[packages]``, so ``--diff`` keeps it when the entry is new. An ignore comment
    on the header covers the entry, and one on ``[packages]`` in a ``Pipfile`` does
    not: no version ever reported the entry there."""
    line = above.count("\n") + 1
    write(name, f'{above}{header}\nversion = "*"\n')
    registry = FakeRegistry({"requests"})
    assert found(check("SLOP001", registry=registry)) == [(name, line, 1)]
    changed = {name: {line, line + 1}}
    assert found(check("SLOP001", registry=registry, changed=changed)) == [(name, line, 1)]

    write(name, f'{above}{header}  # slopfence: ignore\nversion = "*"\n')
    registry = FakeRegistry({"requests"})
    assert check("SLOP001", registry=registry).findings == []
    assert "ghost-lib" not in registry.queries


IGNORED = "  # slopfence: ignore"
OWN_ENTRY = '[tool.poetry.dependencies.corp-auth]\nversion = "^1.2"\nextras = ["saml"]\n'
# A Poetry entry that v0.2 reported on another line than its own, with an ignore
# comment on that line: the file, and the line the entry is reported on today.
WHERE_V02_REPORTED = {
    "the header of the table": (
        f'{POETRY_HEAD}[tool.poetry.dependencies]{IGNORED}\npython = "^3.10"\n\n{OWN_ENTRY}',
        7,
    ),
    "the header, for this rule only": (
        f'{POETRY_HEAD}[tool.poetry.dependencies]{IGNORED}[SLOP001]\nrequests = "*"\n\n'
        + OWN_ENTRY,
        7,
    ),
    "the header of a group": (
        f'{POETRY_HEAD}[tool.poetry.group.dev.dependencies]{IGNORED}\nrequests = "*"\n\n'
        + OWN_ENTRY.replace("poetry.dep", "poetry.group.dev.dep"),
        7,
    ),
    "the header, above a dotted key": (
        f'{POETRY_HEAD}[tool.poetry.dependencies]{IGNORED}\ncorp-auth.version = "^1.2"\n',
        5,
    ),
    "line 1 of a file without the table": (f"{IGNORED.strip()}\n{POETRY_HEAD}{OWN_ENTRY}", 5),
    "the same key in another table": (
        f'{POETRY_HEAD}[tool.poetry.extras]\ncorp-auth = ["saml"]{IGNORED}\n\n{OWN_ENTRY}',
        7,
    ),
}


@pytest.mark.parametrize("where", sorted(WHERE_V02_REPORTED))
def test_an_ignore_comment_where_v02_reported_a_poetry_entry_still_covers_it(tmp_path, where):
    """v0.2 reported ``[tool.poetry.dependencies.name]`` and ``name.version = ...`` on
    the header of the table, so that is where its users put the ignore comment. The
    entry is reported on its own line now, and the comment on the old line still
    keeps its name on the machine. Without the comment it is looked up."""
    text, line = WHERE_V02_REPORTED[where]
    path = tmp_path / "pyproject.toml"
    path.write_text(text, encoding="utf-8")
    registry = FakeRegistry({"requests"})
    assert run([tmp_path], tmp_path, registry, select=["SLOP001"]).findings == []
    assert "corp-auth" not in registry.queries

    path.write_text(text.replace(IGNORED.strip(), "# checked"), encoding="utf-8")
    registry = FakeRegistry({"requests"})
    result = run([tmp_path], tmp_path, registry, select=["SLOP001"])
    assert found(result) == [("pyproject.toml", line, 1)] and "corp-auth" in registry.queries


def test_a_pipfile_entry_with_a_dotted_key_is_on_its_own_line(write, check):
    """``ghost-lib.version = "*"`` is the entry ``ghost-lib``, on that line. A quoted
    key with a dot in it is another entry (the look-alike), on its own line."""
    body = '[packages]\nrequests = "*"\n"ghost.sub" = "*"\nghost = "*"\nghost-lib.version = "*"'
    write("Pipfile", body + "\n")
    registry = FakeRegistry({"requests"})
    result = check("SLOP001", registry=registry)
    assert [(f.subject, f.line) for f in result.findings] == [
        ("ghost-sub", 3),
        ("ghost", 4),
        ("ghost-lib", 5),
    ]
    write("Pipfile", body + "  # slopfence: ignore\n")
    registry = FakeRegistry({"requests"})
    assert [f.line for f in check("SLOP001", registry=registry).findings] == [3, 4]
    assert "ghost-lib" not in registry.queries


def test_a_dependency_file_that_cannot_be_read_declares_nothing(tmp_path, check):
    """A file that is not text is skipped, like a Python file that can't be read."""
    for name in ("requirements.txt", "setup.cfg", "Pipfile"):
        (tmp_path / name).write_bytes(b"ghost-lib\n\xff\xfe\x00")
        assert parse_dependency_file(tmp_path / name, tmp_path).dependencies == []
    registry = FakeRegistry(set())
    assert check("SLOP001", registry=registry).findings == [] and registry.queries == []


INDEX_AND_NAME = f"--extra-index-url {PRIVATE}\ncorp-auth==1.4\n"
NOT_UTF8 = {
    "Latin-1": "# Abh\u00e4ngigkeiten f\u00fcr den Dienst\n".encode("latin-1")
    + INDEX_AND_NAME.encode(),
    "UTF-16": codecs.BOM_UTF16_LE + INDEX_AND_NAME.encode("utf-16-le"),
    "UTF-16 big-endian": codecs.BOM_UTF16_BE + INDEX_AND_NAME.encode("utf-16-be"),
    "UTF-32": codecs.BOM_UTF32_LE + INDEX_AND_NAME.encode("utf-32-le"),
    # Python takes the word after "coding:" for the name of an encoding.
    "coding": b"# no hard-coding: pin versions in constraints\n" + INDEX_AND_NAME.encode(),
}


@pytest.mark.parametrize("encoding", sorted(NOT_UTF8))
def test_an_index_in_a_requirements_file_that_is_not_utf8_still_counts(tmp_path, check, encoding):
    """pip reads a requirements file in the locale's encoding, and in UTF-16 with a
    byte order mark (what ``>`` writes in Windows PowerShell). slopfence reads no
    package from such a file, as before. Its index is still seen, so the files that
    inherit from it keep their names on the machine."""
    (tmp_path / "requirements.txt").write_bytes(NOT_UTF8[encoding])
    cfg = "[options]\ninstall_requires =\n    corp-auth\n"
    (tmp_path / "setup.cfg").write_text(cfg, encoding="utf-8")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert result.findings == [] and registry.queries == []
    assert "a private package index is set in requirements.txt" in result.notes[0]
    assert "secret" not in result.notes[0]


def test_a_requirements_file_that_can_not_be_opened_names_no_index(tmp_path):
    """A path that can't be opened at all (here: a folder) declares nothing and names
    nothing. It does not end the run."""
    (tmp_path / "requirements.txt").mkdir()
    declared = parse_dependency_file(tmp_path / "requirements.txt", tmp_path)
    assert declared.dependencies == [] and not declared.private_index


def test_a_requirements_file_that_is_not_utf8_and_names_no_index_changes_nothing(tmp_path, check):
    """Without an index option, such a file says nothing about the other files."""
    (tmp_path / "requirements.txt").write_bytes(
        "# f\u00fcr pip\ncorp-auth==1.4\n".encode("latin-1")
    )
    cfg = "[options]\ninstall_requires =\n    ghost-lib\n"
    (tmp_path / "setup.cfg").write_text(cfg, encoding="utf-8")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert found(result) == [("setup.cfg", 3, 1)] and result.notes == []
    assert registry.queries == ["ghost-lib"]


def test_pipfile_entries_that_are_not_understood_stay_declared(write, check, not_installed):
    """An entry with an unknown key, or an unquoted dotted name, is not looked up. It
    is still declared, so an import of it is not looked up either."""
    write(
        "Pipfile",
        '[packages]\nodd_shape = {version = "*", something_new = true}\ndotted.name = "*"\n',
    )
    write("app.py", "import odd_shape\nimport dotted\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, check_imports=True)
    assert result.findings == [] and registry.queries == []


NOT_NAMES = ['"my pkg"', '"foo/bar?x=1#frag"', '"../../simple/internal"', '" padded "', '"a\\tb"']
NOT_NAMES += ['"requests[security]"', '"corp-auth>=1"', '"vendor.tar.gz"']


@pytest.mark.parametrize(
    "header",
    ["Pipfile\n[packages]", "pyproject.toml\n[tool.poetry.dependencies]"],
    ids=["Pipfile", "Poetry"],
)
def test_a_table_key_that_is_no_package_name_is_never_sent(write, check, header):
    """A quoted TOML key can hold anything. Only a package name goes to the registry:
    a key with a space would end the run, and one with ``/`` or ``?`` would ask PyPI
    for another address than the one of a package."""
    name, table = header.split("\n")
    body = "".join(f'{key} = "*"\n' for key in [*NOT_NAMES, "ghost-lib", '"zope.interface"'])
    write(name, f"{table}\n{body}")
    registry = FakeRegistry({"zope.interface"})
    result = check("SLOP001", registry=registry)
    assert [f.subject for f in result.findings] == ["ghost-lib"]
    assert sorted(registry.queries) == ["ghost-lib", "zope.interface"]


def test_pipfile_vouches_for_imports(write, check, not_installed):
    """Pipfile packages are declared dependencies: imports of them aren't looked up."""
    write("Pipfile", "[packages]\nurllib3 = '*'\n\n[dev-packages]\niniconfig = '*'\n")
    write("app.py", "import urllib3\nimport iniconfig\nimport ghost_pkg_zz\n")
    registry = FakeRegistry({"urllib3", "iniconfig"})
    result = check("SLOP001", registry=registry, check_imports=True)
    assert found(result) == [("app.py", 3, 1)]
    assert sorted(set(registry.queries)) == ["ghost-pkg-zz", "ghost_pkg_zz", "iniconfig", "urllib3"]


# --- Privacy: what is sent ------------------------------------------------------------

SOURCES = {
    "setup.cfg": "[options]\ninstall_requires =\n    corp-auth\n",
    "setup.py": "from setuptools import setup\nsetup(install_requires=['corp-auth'])\n",
    "Pipfile": "[packages]\ncorp-auth = '*'\n",
    "requirements/base.in": "corp-auth>=1\n",
    "constraints.txt": "corp-auth==1.0\n",
    "dev-requirements.txt": "corp-auth[extra]\n",
}


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_each_source_found_known_offline_unreachable_and_environment(write, check, name):
    """Every new source reports a missing package, and never sends a name it mustn't."""
    write(name, SOURCES[name])
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert [(f.path, f.rule, f.subject) for f in result.findings] == [
        (name, "SLOP001", "corp-auth")
    ]
    assert result.findings[0].message == (
        "Dependency 'corp-auth' does not exist on PyPI (possible hallucination)"
    )

    registry = FakeRegistry(set())
    assert check("SLOP001", registry=registry, known_packages=["corp-*"]).findings == []
    assert registry.queries == []  # known packages are never sent

    registry = FakeRegistry(set())
    switched_off = check("SLOP001", registry=registry, per_file_ignores={name: ["SLOP001"]})
    assert switched_off.findings == [] and registry.queries == []

    assert check("SLOP001").findings == []  # offline
    assert check("SLOP001", registry=DownRegistry()).findings == []  # PyPI can't be reached
    assert check("SLOP001", registry=FakeRegistry({"corp-auth"})).findings == []  # it exists

    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, private_index="the PIP_INDEX_URL variable")
    assert result.findings == [] and registry.queries == []
    assert len(result.notes) == 1 and "made no PyPI lookups" in result.notes[0]


def test_cli_offline_and_default_lookups(tmp_path, monkeypatch, capsys):
    """Through the CLI: only declared names are asked, and --offline asks nothing."""
    for name, body in SOURCES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.replace("corp-auth", f"ghost-{len(name)}"), encoding="utf-8")
    (tmp_path / "app.py").write_text("import corp_secret\n", encoding="utf-8")
    cache = tmp_path / "cache" / "slopfence"
    cache.mkdir(parents=True)
    names = sorted({f"ghost-{len(name)}" for name in SOURCES})
    (cache / "pypi.json").write_text(
        json.dumps({n: [False, 9e12] for n in names}), encoding="utf-8"
    )
    monkeypatch.setenv("SLOPFENCE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)
    asked: list[str] = []
    real_exists = PyPIRegistry.exists

    def exists(self, name):
        """Record every name slopfence asks the registry about."""
        asked.append(name)
        return real_exists(self, name)

    def no_network(self, key):
        """Fail the test if a lookup isn't answered from the pinned cache."""
        raise AssertionError(f"unexpected network lookup for {key!r}")

    monkeypatch.setattr(PyPIRegistry, "exists", exists)
    monkeypatch.setattr(PyPIRegistry, "_query", no_network)

    assert main([".", "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert sorted({f["path"] for f in report["findings"]}) == sorted(SOURCES)
    assert sorted(set(asked)) == names  # the import name stayed local
    asked.clear()
    assert main([".", "--format", "json", "--offline"]) == 0
    assert asked == []


# --- Privacy: a private index somewhere in the project ----------------------------------

# Layouts where the index is written in requirements.txt only, and pip is given both
# files. v0.2 read requirements.txt alone, and sent nothing.
INHERITING = {
    "requirements.in": "requests\ncorp-auth\n",  # pip-compile writes the index into the .txt
    "requirements/base.in": "django\ncorp-billing\n",
    "constraints.txt": "corp-auth==1.4.0\nrequests==2.32.0\n",  # pip install -r ... -c ...
    "dev-requirements.txt": "corp-testkit\npytest\n",
    "Pipfile": '[packages]\ncorp-auth = "*"\n',
    "setup.py": 'from setuptools import setup\nsetup(name="x", install_requires=["corp-auth"])\n',
    "setup.cfg": "[options]\ninstall_requires =\n    corp-auth\n",
}


@pytest.mark.parametrize("name", sorted(INHERITING))
def test_a_private_index_in_the_project_covers_every_file_v02_did_not_read(write, tmp_path, name):
    """A file that names no index may be installed with one that does. So its names
    are not sent either, also when it is the only file that is checked (pre-commit)."""
    path = write(name, INHERITING[name])
    write("deploy/requirements.txt", INDEX + "corp-auth==1.4.0\n")
    for paths in ([tmp_path], [path]):
        registry = FakeRegistry(set())
        result = run(paths, tmp_path, registry, select=["SLOP001"])
        assert result.findings == [] and registry.queries == []
        assert result.notes == [
            f"SLOP001 didn't look up the dependencies in {name}: a private package index is "
            "set in deploy/requirements.txt, so names missing from PyPI may be private "
            "packages. If that index only mirrors PyPI, set detect-private-index = false."
        ]
        assert "secret" not in result.notes[0]


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("pyproject.toml", f'[[tool.uv.index]]\nname = "corp"\nurl = "{PRIVATE}"\n'),
        ("Pipfile", f'[[source]]\nname = "corp"\nurl = "{PRIVATE}"\n'),
        ("tools/Pipfile.lock", json.dumps({"_meta": {"sources": [{"url": PRIVATE}]}})),
        ("requirements/prod.in", f"--index-url {PRIVATE}\ncorp-core>=1\n"),
    ],
)
def test_every_file_that_can_name_an_index_is_inherited_from(write, check, name, body):
    """The index may be in pyproject.toml, a Pipfile, a Pipfile.lock or any requirements file."""
    write(name, body)
    write("setup.cfg", "[options]\ninstall_requires =\n    corp-auth\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert result.findings == [] and registry.queries == []
    assert f"a private package index is set in {name}" in result.notes[0]


UV_INDEX = f'[[tool.uv.index]]\nname = "corp"\nurl = "{PRIVATE}"\n'
PIPFILE_INDEX = f'[[source]]\nname = "corp"\nurl = "{PRIVATE}"\n'
LOCKED_INDEX = json.dumps({"_meta": {"sources": [{"url": PRIVATE}]}, "default": {}})
# Files that name a private index and can't be read: because of one more line, or
# because they are not UTF-8 (one Latin-1 letter in a comment or a name, or UTF-16,
# which is what ``>`` writes in Windows PowerShell).
UNREADABLE = {
    "pyproject.toml": UV_INDEX + "this line is not TOML\n",
    "tools/pyproject.toml": "x = " + "[" * 5000 + "]" * 5000 + "\n",
    "Pipfile": PIPFILE_INDEX + "[packages\n",
    "Pipfile.lock": '{"_meta": {"sources": [{"url": "' + PRIVATE + '"}]}, "default": {',
    "deploy/Pipfile.lock": json.dumps({"_meta": {"sources": [{"url": PRIVATE}]}, "default": 3}),
    "latin/pyproject.toml": (UV_INDEX + "# Jos\u00e9\n").encode("latin-1"),
    "utf16/pyproject.toml": UV_INDEX.encode("utf-16"),
    "latin/Pipfile": ("# Jos\u00e9\n" + PIPFILE_INDEX).encode("latin-1"),
    "utf16/Pipfile": PIPFILE_INDEX.encode("utf-16"),
    "latin/Pipfile.lock": LOCKED_INDEX.replace("{}", '{"Jos\u00e9": {}}').encode("latin-1"),
    "utf16/Pipfile.lock": LOCKED_INDEX.encode("utf-16"),
}


@pytest.mark.parametrize("name", sorted(UNREADABLE))
def test_a_file_that_may_set_an_index_and_can_not_be_read_is_inherited_from(write, tmp_path, name):
    """A ``pyproject.toml``, ``Pipfile`` or ``Pipfile.lock`` that can't be read (its
    reader turns it down, or it is not UTF-8) may set a private index that nobody can
    see. So the files that would inherit it are held back, also when one is checked
    alone, and the note names the file."""
    content = UNREADABLE[name]
    write(name, "").write_bytes(content if isinstance(content, bytes) else content.encode())
    path = write("setup.cfg", "[options]\ninstall_requires =\n    corp-x\n")
    for paths in ([tmp_path], [path]):
        registry = FakeRegistry(set())
        result = run(paths, tmp_path, registry, select=["SLOP001"])
        assert result.findings == [] and registry.queries == []
        assert result.notes == [
            f"SLOP001 didn't look up the dependencies in setup.cfg: {name} could not be read "
            "and may set a private package index, so names missing from PyPI may be private "
            "packages. Repair or exclude that file, or set detect-private-index = false."
        ]


@pytest.mark.parametrize("name", ["uv.lock", "poetry.lock", "constraints.txt", "setup.cfg"])
@pytest.mark.parametrize("content", [b"[[package\nthis line is not TOML\n", b"# Jos\xe9\n"])
def test_a_broken_file_that_can_set_no_index_holds_nothing_back(write, check, name, content):
    """The look-alike cases, broken or not UTF-8. A lock file other than
    ``Pipfile.lock`` records no index, a requirements file has no syntax to turn
    down, and a ``setup.cfg`` only speaks for itself."""
    write(f"deploy/{name}", "").write_bytes(content)
    write("setup.py", 'from setuptools import setup\nsetup(install_requires=["ghost-lib"])\n')
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert [f.subject for f in result.findings] == ["ghost-lib"] and result.notes == []


def test_an_unreadable_file_is_inherited_from_like_one_that_names_an_index(write, check):
    """What holds for a private index holds for a file that may set one: the files v0.2
    read speak for themselves, an excluded file doesn't count, and
    ``detect-private-index = false`` switches it off. Imports are looked up as before."""
    write("vendor/pyproject.toml", UNREADABLE["pyproject.toml"])
    write("requirements.txt", "ghost-one\n")
    write("constraints.txt", "ghost-two==1.0\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert [f.subject for f in result.findings] == ["ghost-one"]
    assert registry.queries == ["ghost-one"] and "constraints.txt: vendor/py" in result.notes[0]
    for switch in ({"exclude": ["vendor"]}, {"inherit_index": False}):
        result = check("SLOP001", registry=FakeRegistry(set()), **switch)
        assert [f.subject for f in result.findings] == ["ghost-two", "ghost-one"]
        assert result.notes == []
    write("app.py", "import ghost_mod_zz\n")
    result = check("SLOP001", registry=FakeRegistry(set()), check_imports=True)
    assert [f.subject for f in result.findings] == ["ghost_mod_zz", "ghost-one"]


def test_the_note_names_the_file_that_sets_an_index_before_one_that_may(write, check):
    """With a private index that can be seen, the note needs no file that can't be read."""
    write("pyproject.toml", UNREADABLE["pyproject.toml"])
    write("deploy/requirements.txt", INDEX + "corp-auth\n")
    write("setup.cfg", "[options]\ninstall_requires =\n    corp-x\n")
    result = check("SLOP001", registry=FakeRegistry(set()))
    assert result.findings == []
    assert "a private package index is set in deploy/requirements.txt, so" in result.notes[0]


def test_the_files_v02_read_do_not_inherit(write, check):
    """``requirements*.txt`` and ``pyproject.toml`` speak for themselves, as in v0.2: an
    index in another file does not switch them off."""
    write("deploy/requirements.txt", INDEX + "corp-auth\n")
    write("requirements.txt", "ghost-one\n")
    write("requirements-dev.txt", "ghost-two\n")
    write("pyproject.toml", '[project]\nname = "demo"\ndependencies = ["ghost-three"]\n')
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert [f.subject for f in result.findings] == ["ghost-three", "ghost-two", "ghost-one"]
    assert "corp-auth" not in registry.queries and result.notes == []


def test_an_index_in_an_excluded_file_is_not_inherited(write, check):
    """``exclude`` is how a user says a folder is not part of the project."""
    write("vendor/tool/requirements.txt", INDEX + "x\n")
    write("setup.cfg", "[options]\ninstall_requires =\n    ghost-lib\n")
    covered = check("SLOP001", registry=FakeRegistry(set()))
    assert covered.findings == [] and len(covered.notes) == 1
    result = check("SLOP001", registry=FakeRegistry(set()), exclude=["vendor"])
    assert found(result) == [("setup.cfg", 3, 1)] and result.notes == []


@pytest.mark.parametrize("folder", ["build", "dist", "env", "venv", ".tox", "x.egg-info"])
def test_an_index_in_a_folder_that_is_never_read_is_not_inherited(write, check, folder):
    """Build output and virtual environments are never walked, so a requirements file
    in one of them says nothing about the project."""
    write(f"{folder}/requirements.txt", INDEX + "x\n")
    write("setup.cfg", "[options]\ninstall_requires =\n    ghost-lib\n")
    result = check("SLOP001", registry=FakeRegistry(set()))
    assert found(result) == [("setup.cfg", 3, 1)] and result.notes == []


def test_detect_private_index_false_switches_inheritance_off(write, check):
    """An index that only mirrors PyPI must not switch the new files off. The file
    that names the index stays private."""
    write("requirements.txt", INDEX + "corp-auth\n")
    write("setup.cfg", "[options]\ninstall_requires =\n    ghost-lib\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, inherit_index=False)
    assert found(result) == [("setup.cfg", 3, 1)] and result.notes == []
    assert registry.queries == ["ghost-lib"]
    # Imports are another matter: the switch does not bring their lookups back.
    write("app.py", "import ghost_mod\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, inherit_index=False, check_imports=True)
    assert found(result) == [("setup.cfg", 3, 1)] and registry.queries == ["ghost-lib"]
    assert "didn't look up imports on PyPI: requirements.txt uses" in result.notes[0]


def test_cli_passes_detect_private_index_on(tmp_path, monkeypatch, capsys):
    """``--no-detect-private-index`` and the config key reach the inheritance rule."""
    (tmp_path / "requirements.txt").write_text(INDEX + "corp-auth\n", encoding="utf-8")
    (tmp_path / "constraints.txt").write_text("ghost-pin==1.0\n", encoding="utf-8")
    cache = tmp_path / "cache" / "slopfence"
    cache.mkdir(parents=True)
    (cache / "pypi.json").write_text(json.dumps({"ghost-pin": [False, 9e12]}), encoding="utf-8")
    monkeypatch.setenv("SLOPFENCE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(PyPIRegistry, "_query", lambda self, key: pytest.fail(f"lookup of {key}"))
    monkeypatch.chdir(tmp_path)
    assert main([".", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["findings"] == [] and "constraints.txt" in report["notes"][0]
    assert main([".", "--format", "json", "--no-detect-private-index"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert [f["path"] for f in report["findings"]] == ["constraints.txt"]
    assert report["notes"] == []
    (tmp_path / "pyproject.toml").write_text(
        "[tool.slopfence]\ndetect-private-index = false\n", encoding="utf-8"
    )
    assert main([".", "--format", "json"]) == 1
    capsys.readouterr()


PRIVATE_LOCK = json.dumps({"_meta": {"sources": [{"name": "corp", "url": PRIVATE}]}})


@pytest.mark.parametrize(
    ("files", "listed"),
    [
        (
            {
                "Pipfile": f'[[source]]\nname = "corp"\nurl = "{PRIVATE}"\n',
                "deploy/requirements.txt": INDEX + "corp-b\n",
                "deploy-eu/requirements.txt": INDEX + "corp-b\n",
            },
            "Pipfile, deploy-eu/requirements.txt, deploy/requirements.txt",
        ),
        (
            dict.fromkeys(
                ["backend/Pipfile.lock", "Pipfile.lock", "backend-eu/Pipfile.lock"], PRIVATE_LOCK
            ),
            "Pipfile.lock, backend-eu/Pipfile.lock, backend/Pipfile.lock",
        ),
    ],
    ids=["dependency files", "lock files"],
)
def test_a_note_lists_files_in_the_order_of_their_paths_as_text(
    write, check, not_installed, files, listed
):
    """The files in a note come in the order of their project-relative paths as text:
    the order of the findings, and the same on every operating system. ``Path``
    objects compare folder by folder, and without case on Windows, where ``Pipfile``
    came after ``deploy/requirements.txt``."""
    for name, body in files.items():
        write(name, body)
    write("setup.cfg", "[options]\ninstall_requires = corp-c\n")
    write("app.py", "import corp_mod_zz\n")
    result = check("SLOP001", registry=FakeRegistry(set()), check_imports=True)
    assert result.findings == []
    assert f"didn't look up imports on PyPI: {listed} uses" in result.notes[0]
    assert f"a private package index is set in {listed}, so" in result.notes[1]


def test_the_note_names_only_checked_files_and_stays_short(write, tmp_path):
    """The note lists the files whose names were held back in this run, three at most,
    and comes with no run that asks PyPI nothing anyway."""
    write("requirements.txt", INDEX + "corp-auth\n")
    for n in range(5):
        write(f"pkg{n}/setup.cfg", "[options]\ninstall_requires =\n    corp-auth\n")
    write("empty/setup.cfg", "[options]\npackages = find:\n")
    write("known/setup.cfg", "[options]\ninstall_requires =\n    known-lib\n")
    known = ["known-*"]
    result = run([tmp_path / "pkg3"], tmp_path, FakeRegistry(set()), known_packages=known)
    assert "the dependencies in pkg3/setup.cfg: a private" in result.notes[0]
    result = run([tmp_path], tmp_path, FakeRegistry(set()), known_packages=known)
    assert "in pkg0/setup.cfg, pkg1/setup.cfg, pkg2/setup.cfg and 2 more files:" in result.notes[0]
    # Nothing was held back in a file without names to look up.
    paths = [tmp_path / "empty", tmp_path / "known"]
    result = run(paths, tmp_path, FakeRegistry(set()), known_packages=known)
    assert result.notes == []
    # Offline, and with a private index in the environment, nothing is looked up anyway.
    assert run([tmp_path], tmp_path, DownRegistry(), select=["SLOP001"]).notes != []
    assert run([tmp_path], tmp_path, OfflineRegistry(), select=["SLOP001"]).notes == []
    result = run([tmp_path], tmp_path, FakeRegistry(set()), private_index="the X variable")
    assert len(result.notes) == 1 and "made no PyPI lookups" in result.notes[0]


def test_dependency_links_stop_import_lookups(write, check, not_installed):
    """``dependency_links`` say packages may come from elsewhere: that file's packages
    are private, and with ``check_imports`` unknown imports may be private too."""
    write(
        "setup.py",
        "from setuptools import setup\n"
        "setup(install_requires=['corp-a'], dependency_links=['https://corp.example/d'])\n",
    )
    write("requirements.txt", "ghost-lib\n")
    write("app.py", "import corp_mod_zz\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, check_imports=True)
    # It only speaks for itself: requirements.txt is still checked.
    assert found(result) == [("requirements.txt", 1, 1)] and registry.queries == ["ghost-lib"]
    assert result.notes == [
        "SLOP001 didn't look up imports on PyPI: setup.py uses a private package index, so "
        "unknown imports may be private packages."
    ]


REQUIRES = "install_requires =\n    corp-auth\nsetup_requires = corp-build\n"


@pytest.mark.parametrize(
    "cfg",
    [
        f"[easy_install]\nindex_url = {PRIVATE}\n\n[options]\n{REQUIRES}",
        f"[easy_install]\nindex-url = {PRIVATE}\n\n[options]\n{REQUIRES}",
        f"[easy_install]\nfind_links = https://corp.example/wheels\n\n[options]\n{REQUIRES}",
        f"[easy_install]\nFind-Links =\n    ./wheels\n\n[options]\n{REQUIRES}",
        f"[options]\ndependency_links = https://corp.example/links\n{REQUIRES}",
    ],
    ids=["index_url", "index-url", "find_links", "Find-Links", "dependency_links"],
)
def test_a_setup_cfg_that_names_a_source_covers_the_setup_py_next_to_it(write, tmp_path, cfg):
    """setuptools reads ``setup.cfg`` and the ``setup.py`` next to it as one
    distribution. An index or links in the first hold for the names in the second,
    also when only one of the two is checked. A ``setup.py`` elsewhere is not covered."""
    cfg_path = write("pkg/setup.cfg", cfg)
    setup = "from setuptools import setup\nsetup(name='x', install_requires=['corp-core'])\n"
    py_path = write("pkg/setup.py", setup)
    for paths in ([tmp_path], [cfg_path], [py_path]):
        registry = FakeRegistry(set())
        result = run(paths, tmp_path, registry, select=["SLOP001"])
        assert result.findings == [] and registry.queries == [] and result.notes == []
    write("other/setup.py", setup.replace("corp-core", "ghost-lib"))
    registry = FakeRegistry(set())
    result = run([tmp_path], tmp_path, registry, select=["SLOP001"])
    assert found(result) == [("other/setup.py", 2, 35)] and registry.queries == ["ghost-lib"]


def test_a_setup_py_with_links_covers_the_setup_cfg_next_to_it(write, check):
    """The same the other way round: ``dependency_links`` passed to ``setup()``."""
    write(
        "setup.py", "from setuptools import setup\nsetup(dependency_links=['https://c.example'])\n"
    )
    write("setup.cfg", f"[options]\n{REQUIRES}")
    registry = FakeRegistry(set())
    assert check("SLOP001", registry=registry).findings == [] and registry.queries == []


def test_an_easy_install_index_that_is_pypi_changes_nothing(write, check):
    """``index_url`` with PyPI's own address, or with no value, names no other source."""
    for index in ("index_url = https://pypi.org/simple", "index_url ="):
        write("setup.cfg", f"[easy_install]\n{index}\n\n[options]\ninstall_requires = ghost-lib\n")
        registry = FakeRegistry(set())
        assert found(check("SLOP001", registry=registry)) == [("setup.cfg", 5, 1)]
        assert registry.queries == ["ghost-lib"]


@pytest.mark.parametrize("links", ["[]", "()", "''", "None"])
def test_empty_dependency_links_name_no_source(write, check, not_installed, links):
    """``dependency_links=[]`` is what a project template leaves behind. It holds no
    link, so the file's packages are looked up, and imports too."""
    write(
        "setup.py",
        "from setuptools import setup\n"
        f"setup(name='demo', install_requires=['ghost-a'], dependency_links={links})\n",
    )
    write("app.py", "import ghost_mod_zz\n")
    result = check("SLOP001", registry=FakeRegistry(set()), check_imports=True)
    assert [f.subject for f in result.findings] == ["ghost_mod_zz", "ghost-a"]
    assert result.notes == []


@pytest.mark.parametrize("links", ["LINKS", "find_links()", "[url]", "['']", "[] + extra"])
def test_dependency_links_that_may_hold_a_link_name_a_source(write, check, links):
    """Anything but a literal that is certainly empty may hold a link."""
    write("setup.py", f"from setuptools import setup\nsetup(dependency_links={links})\n")
    write("setup.cfg", "[options]\ninstall_requires = corp-auth\n")
    registry = FakeRegistry(set())
    assert check("SLOP001", registry=registry).findings == [] and registry.queries == []


def test_lock_files_are_only_read_for_what_the_run_needs(write, check, monkeypatch):
    """Without ``check_imports`` nobody asks for the names in a lock file. Only
    ``Pipfile.lock`` is read then, because it can name an index."""
    from slopfence.detectors import packages

    read = []
    real = packages.parse_lock_file
    monkeypatch.setattr(
        packages, "parse_lock_file", lambda path: read.append(path.name) or real(path)
    )
    write("uv.lock", '[[package]]\nname = "idna"\n')
    write("poetry.lock", '[[package]]\nname = "six"\n')
    write("Pipfile.lock", json.dumps({"_meta": {"sources": []}, "default": {"certifi": {}}}))
    write("requirements.txt", "ghost-lib\n")
    assert found(check("SLOP001", registry=FakeRegistry(set()))) == [("requirements.txt", 1, 1)]
    assert read == ["Pipfile.lock"]
    read.clear()
    check("SLOP001", registry=FakeRegistry(set()), check_imports=True)
    assert sorted(read) == ["Pipfile.lock", "poetry.lock", "uv.lock"]


# --- The project's own packages ---------------------------------------------------------

OWN_NAMES = {
    "packages/core/setup.py": "from setuptools import setup\nsetup(name='Acme_Core')\n",
    "packages/util/setup.cfg": "[metadata]\nname = acme.util\n",
    "packages/cli/pyproject.toml": '[project]\nname = "acme-cli"\n',
    "packages/old/pyproject.toml": '[tool.poetry]\nname = "acme-old"\n',
}
SIBLINGS = "acme-core\nacme-util\nacme_cli\nACME-old\nrequests\nghost-lib\n"


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("requirements.txt", SIBLINGS),
        ("requirements/base.in", SIBLINGS),
        ("setup.cfg", "[options]\ninstall_requires =\n    " + SIBLINGS.replace("\n", "\n    ")),
        (
            "setup.py",
            f"from setuptools import setup\nsetup(install_requires={SIBLINGS.split()!r})\n",
        ),
        ("Pipfile", "[packages]\n" + "".join(f'{n} = "*"\n' for n in SIBLINGS.split())),
        (
            "pyproject.toml",
            f'[project]\nname = "app"\ndependencies = {json.dumps(SIBLINGS.split())}\n',
        ),
    ],
)
def test_packages_of_the_same_repository_are_never_looked_up(write, tmp_path, name, body):
    """A dependency that another ``setup.py``, ``setup.cfg`` or ``pyproject.toml`` of the
    project defines is not a hallucination, and its name is not sent. That holds for
    every source, and when only one file is checked."""
    for rel, text in OWN_NAMES.items():
        write(rel, text)
    path = write(f"app/{name}", body)
    for paths in ([tmp_path], [path]):
        registry = FakeRegistry({"requests"})
        result = run(paths, tmp_path, registry, select=["SLOP001"])
        assert [f.subject for f in result.findings] == ["ghost-lib"]
        assert sorted(set(registry.queries)) == ["ghost-lib", "requests"]


def test_own_names_count_in_excluded_folders_too(write, check):
    """An excluded package is still a package of this repository."""
    write("vendor/acme/setup.py", "from setuptools import setup\nsetup(name='acme-vendored')\n")
    write("requirements.txt", "acme-vendored\n")
    registry = FakeRegistry(set())
    assert check("SLOP001", registry=registry, exclude=["vendor"]).findings == []
    assert registry.queries == []


# --- Fixture packages under a test folder -----------------------------------------------

FIXTURE_PACKAGES = {
    "tests/data/packages/requires_thing/setup.py": (
        "from setuptools import setup\n"
        "setup(name='requires_thing', version='1.0', install_requires=['thing-fixture-dep'])\n"
    ),
    "tests/fixtures/proj/setup.cfg": (
        "[metadata]\nname = fixture\n\n[options]\ninstall_requires =\n    fixture-dep-b\n"
    ),
    "pkg/test/pipenv/Pipfile": '[packages]\nfixture-dep-c = "*"\n',
}


@pytest.mark.parametrize("name", sorted(FIXTURE_PACKAGES))
def test_a_fixture_package_under_a_test_folder_is_not_looked_up(write, tmp_path, name):
    """A ``setup.py``, ``setup.cfg`` or ``Pipfile`` under a test folder defines a
    package that only the tests use. Its made-up dependencies are not sent and not
    reported, also when the file is the only one that is checked."""
    path = write(name, FIXTURE_PACKAGES[name])
    for paths in ([tmp_path], [path]):
        registry = FakeRegistry(set())
        result = run(paths, tmp_path, registry, select=["SLOP001"])
        assert result.findings == [] and registry.queries == [] and result.notes == []


@pytest.mark.parametrize(
    ("name", "body"),
    [
        # A requirements file for the tests is a real install list, and v0.2 read it.
        ("tests/requirements.txt", "ghost-lib==1.0\n"),
        ("tests/constraints.txt", "ghost-lib==1.0\n"),
        ("tests/requirements/ci.in", "ghost-lib==1.0\n"),
        # v0.2 read every pyproject.toml.
        (
            "tests/fixtures/pp/pyproject.toml",
            '[project]\nname = "pp"\ndependencies = ["ghost-lib"]\n',
        ),
        # Folders that only sound like a test folder.
        ("testing/setup.cfg", "[options]\ninstall_requires =\n    ghost-lib\n"),
        ("latest/Pipfile", '[packages]\nghost-lib = "*"\n'),
    ],
)
def test_a_file_under_a_test_folder_that_is_no_fixture_package_is_still_checked(
    write, check, name, body
):
    """The look-alike cases: other kinds of file under a test folder, and a package in
    a folder that is no test folder."""
    write(name, body)
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert [(f.path, f.subject) for f in result.findings] == [(name, "ghost-lib")]


def test_a_fixture_package_still_counts_for_the_rest_of_the_project(write, check):
    """A fixture package is still read. Its name is a package of this repository, a
    package it takes from git is the same package elsewhere, and a private index it
    sets covers the files that inherit one. The note leaves the fixture out."""
    write("tests/fixtures/proj/setup.cfg", "[metadata]\nname = fixture-pkg\n")
    write("tests/fixtures/app/Pipfile", '[packages]\ncorp-auth = {git = "https://x.example/c"}\n')
    write("tests/fixtures/other/setup.py", FIXTURE_PACKAGES[sorted(FIXTURE_PACKAGES)[1]])
    write("requirements.txt", "fixture-pkg\ncorp-auth\nghost-lib\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert [f.subject for f in result.findings] == ["ghost-lib"]
    assert registry.queries == ["ghost-lib"]

    write("tests/fixtures/private/Pipfile", f'[[source]]\nname = "corp"\nurl = "{PRIVATE}"\n')
    write("setup.cfg", "[options]\ninstall_requires =\n    corp-x\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert "corp-x" not in registry.queries and "thing-fixture-dep" not in registry.queries
    assert result.notes == [
        "SLOP001 didn't look up the dependencies in setup.cfg: a private package index is "
        "set in tests/fixtures/private/Pipfile, so names missing from PyPI may be private "
        "packages. If that index only mirrors PyPI, set detect-private-index = false."
    ]


def test_a_named_file_outside_the_project_is_no_fixture_for_the_folders_above_it(tmp_path):
    """Only the folders inside the project make a test folder. A file that is named on
    the command line and lies outside the project is checked, whatever the folders
    above it are called. So is a project that lives under a folder named tests."""
    root = tmp_path / "tests" / "project"
    outside = tmp_path / "tests" / "other"
    for folder in (root, outside):
        folder.mkdir(parents=True)
        (folder / "setup.cfg").write_text(
            "[options]\ninstall_requires =\n    ghost-lib\n", encoding="utf-8"
        )
    registry = FakeRegistry(set())
    result = run([root, outside / "setup.cfg"], root, registry, select=["SLOP001"])
    named = (outside / "setup.cfg").resolve().as_posix()  # reported with its full path
    assert sorted(f.path for f in result.findings) == sorted([named, "setup.cfg"])


# --- A package that one file takes from a place of its own -----------------------------


def one_cell_notebook(line):
    """The text of a notebook whose one code cell holds ``line``."""
    return json.dumps({"nbformat": 4, "cells": [{"cell_type": "code", "source": [line]}]})


# Each file says where ``corp-auth`` comes from, in its own way.
SOURCED = {
    "direct reference": ("requirements.txt", f"corp-auth @ {GIT}@v1\nrequests\n"),
    "egg": ("requirements.txt", f"-e {GIT}#egg=corp-auth\nrequests\n"),
    "Pipfile git": ("Pipfile", f'[packages]\ncorp-auth = {{git = "{GIT}"}}\nrequests = "*"\n'),
    "pyproject URL": (
        "pyproject.toml",
        '[project]\nname = "demo"\n'
        'dependencies = ["corp-auth @ https://corp.example/corp_auth-1.0.whl", "requests"]\n',
    ),
    "uv source": (
        "pyproject.toml",
        '[project]\nname = "demo"\ndependencies = ["corp-auth", "requests"]\n\n'
        f'[tool.uv.sources]\ncorp-auth = {{ git = "{GIT}" }}\n',
    ),
    "uv explicit index": (
        "pyproject.toml",
        '[project]\nname = "demo"\ndependencies = ["corp-auth", "requests"]\n\n'
        f'[[tool.uv.index]]\nname = "corp"\nurl = "{PRIVATE}"\nexplicit = true\n\n'
        '[tool.uv.sources]\ncorp-auth = { index = "corp" }\n',
    ),
    "Poetry explicit source": (
        "pyproject.toml",
        '[tool.poetry]\nname = "demo"\n\n[tool.poetry.dependencies]\nrequests = "*"\n'
        'corp-auth = { version = "^1", source = "corp" }\n\n'
        f'[[tool.poetry.source]]\nname = "corp"\nurl = "{PRIVATE}"\npriority = "explicit"\n',
    ),
    # A direct reference without "://": a path, and Hatch's way to name one.
    "pyproject path": (
        "pyproject.toml",
        '[project]\nname = "demo"\ndependencies = ["corp-auth @ file:../corp-auth", "requests"]\n',
    ),
    "pyproject Hatch path": (
        "pyproject.toml",
        '[project]\nname = "demo"\n\n[project.optional-dependencies]\n'
        'dev = ["corp-auth @ {root:parent:uri}/corp-auth", "requests"]\n',
    ),
    "dependency group path": (
        "pyproject.toml",
        '[project]\nname = "demo"\n\n[dependency-groups]\n'
        'dev = ["corp-auth[sso] @ ./vendor/corp-auth", "requests"]\n',
    ),
    # A URL where pip takes none. v0.2 kept such a name on the machine.
    "pyproject URL after a version": (
        "pyproject.toml",
        '[project]\nname = "demo"\n'
        'dependencies = ["corp-auth==1.0 @ https://corp.example/corp_auth-1.0.whl", "requests"]\n',
    ),
    "requirements URL after a version": (
        "requirements.txt",
        "corp-auth==1.0 @ https://corp.example/corp_auth-1.0.whl\nrequests\n",
    ),
    "setup.cfg direct reference": (
        "setup.cfg",
        f"[options]\ninstall_requires =\n    corp-auth @ {GIT}\n    requests\n",
    ),
    # A file that is taken for a document: none of its names is looked up, and what
    # it says about a package still counts.
    "document with an underline": (
        "requirements.txt",
        f"Internal packages\n-----------------\ncorp-auth @ {GIT}\nrequests==2.32.0\n",
    ),
    "document with an underline and an egg": (
        "requirements.txt",
        f"Internal packages\n-----------------\n-e {GIT}#egg=corp-auth\n",
    ),
    "document with more prose": (
        "requirements.txt",
        f"This file lists what we use.\nIt is kept by hand.\nAsk first.\ncorp-auth @ {GIT}\n",
    ),
    # A notebook is read in every run, also when it is not among the checked files.
    "notebook egg": ("nb/train.ipynb", one_cell_notebook(f"%pip install {GIT}#egg=corp-auth\n")),
    "notebook direct reference": (
        "nb/setup.ipynb",
        one_cell_notebook(f"!pip install requests 'corp-auth @ {GIT}'\n"),
    ),
    "notebook path": (
        "nb/local.ipynb",
        one_cell_notebook('%pip install requests "corp-auth @ file:../corp-auth"\n'),
    ),
    "notebook path in one word": (
        "nb/local.ipynb",
        one_cell_notebook("!pip install corp-auth@file:../corp-auth\n%pip install corp-auth\n"),
    ),
}
# The files that name the package again, without saying where it comes from.
NAMED_AGAIN = {
    "legacy/setup.py": (
        'from setuptools import setup\nsetup(name="old", install_requires=["corp-auth>=1"])\n'
    ),
    "tools/setup.cfg": "[options]\ninstall_requires =\n    corp-auth\n",
    "constraints.txt": "corp-auth==1.2.3\nrequests==2.32.0\n",
    "requirements.in": "Corp_Auth\nrequests\n",
    "tools/Pipfile": '[packages]\ncorp-auth = "*"\n',
    "docs/requirements.txt": "corp-auth\n",
    "sub/pyproject.toml": '[project]\nname = "sub"\ndependencies = ["corp-auth"]\n',
}


@pytest.mark.parametrize("layout", sorted(SOURCED))
def test_a_package_with_a_source_of_its_own_is_not_looked_up_in_any_file(write, tmp_path, layout):
    """One file installs ``corp-auth`` from git, a URL or an index it names. The same
    name in another file is the same package, so it is never sent and never reported:
    in the files v0.2 read, in the new ones, and when a single file is checked."""
    name, body = SOURCED[layout]
    write(name, body)
    again = [write(rel, text) for rel, text in NAMED_AGAIN.items()]
    for paths in ([tmp_path], *([path] for path in again)):
        registry = FakeRegistry({"requests"})
        result = run(paths, tmp_path, registry, select=["SLOP001"])
        assert result.findings == [] and set(registry.queries) <= {"requests"}
        assert result.notes == []


PROSE = "This file lists what we use.\nIt is kept by hand.\nAsk the team first.\n"
# Requirements files that are taken for a document, and name ``corp-auth`` all the same.
DECLARING_DOCUMENTS = {
    "an underline and an ignore comment": f"Internal\n--------\ncorp-auth==1.0{IGNORED}\n",
    "an underline and a git entry": f"Internal\n--------\ncorp-auth @ {GIT}\n",
    "a bullet and a links option": "-f ./wheels\ncorp-auth==1.0\n- 4 GB RAM\n",
    "prose and an ignore comment": PROSE + "corp-auth==1.0  # slopfence: ignore\n",
    "prose and a git entry": PROSE + f"corp-auth @ {GIT}\n",
}


@pytest.mark.parametrize("document", sorted(DECLARING_DOCUMENTS))
def test_a_package_that_a_document_declares_is_not_looked_up_as_an_import(
    write, check, not_installed, document
):
    """None of a document's names is looked up, and each still counts as declared: an
    import of such a package was never sent, and is not now. Taking a real
    requirements file for a document can so only keep names on the machine."""
    write("requirements.txt", DECLARING_DOCUMENTS[document])
    write("app.py", "import corp_auth\nimport ghost_mod_zz\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, check_imports=True)
    assert [f.subject for f in result.findings] == ["ghost_mod_zz"]
    assert registry.queries == ["ghost-mod-zz", "ghost_mod_zz"]


# What ``pip freeze`` prints for a project with two packages from git.
FROZEN = (
    f"certifi==2024.8.30\r\ncorp-auth @ {GIT}@3f2a9c1\r\n"
    f"-e {GIT.replace('corp-auth', 'corp-kit')}#egg=corp-kit\r\nrequests==2.32.3\r\n"
)
FROZEN_NOT_UTF8 = {
    "UTF-16": codecs.BOM_UTF16_LE + FROZEN.encode("utf-16-le"),
    "Latin-1": "# Abh\u00e4ngigkeiten\r\n".encode("latin-1") + FROZEN.encode(),
}


@pytest.mark.parametrize("encoding", sorted(FROZEN_NOT_UTF8))
def test_a_source_in_a_requirements_file_that_is_not_utf8_still_counts(tmp_path, encoding):
    """``pip freeze > requirements.txt`` in Windows PowerShell writes UTF-16. The
    packages of such a file are not looked up, as in v0.2. An entry from git still
    says where its package comes from, so the same name in another file is not
    sent, also when that file is checked alone."""
    (tmp_path / "requirements.txt").write_bytes(FROZEN_NOT_UTF8[encoding])
    setup = tmp_path / "setup.py"
    setup.write_text(
        "from setuptools import setup\n"
        'setup(name="demo", install_requires=["corp-auth", "corp-kit", "requests"])\n',
        encoding="utf-8",
    )
    for paths in ([tmp_path], [setup]):
        registry = FakeRegistry({"requests"})
        result = run(paths, tmp_path, registry, select=["SLOP001"])
        # Asked once: for setup.py. The names in the unread file are not asked about.
        assert result.findings == [] and registry.queries == ["requests"]


NO_REQUIREMENT_STRINGS = [
    "python>=3.9",
    "vendor.tar.gz",
    "2024.01.15",
    "foo bar baz",
    "f" * 32,
    "x = 1",
]
PYPROJECT_ARRAYS = {
    "project": '[project]\nname = "demo"\ndependencies = {}\n',
    "optional": '[project]\nname = "demo"\n\n[project.optional-dependencies]\ndev = {}\n',
    "group": '[project]\nname = "demo"\n\n[dependency-groups]\ndev = {}\n',
}


@pytest.mark.parametrize("array", sorted(PYPROJECT_ARRAYS))
def test_a_pyproject_string_is_read_like_a_requirement_in_any_other_file(write, check, array):
    """One reader for requirement strings: what is no requirement in a requirements
    file (the interpreter, a file name, a date, prose, a checksum) is none in
    ``pyproject.toml`` either. It is not sent, and a real requirement next to it is."""
    specs = [*NO_REQUIREMENT_STRINGS, "ghost-lib>=1", "requests[socks] ; python_version < '3.13'"]
    write("pyproject.toml", PYPROJECT_ARRAYS[array].format(json.dumps(specs)))
    registry = FakeRegistry({"requests"})
    result = check("SLOP001", registry=registry)
    assert [f.subject for f in result.findings] == ["ghost-lib"]
    assert registry.queries == ["ghost-lib", "requests"]


UV_SOURCE = f'\n[tool.uv.sources]\ncorp-auth = {{ git = "{GIT}" }}\n'
# A file that names ``corp-auth`` in a string that is no requirement (a single "=",
# Poetry's "^", a bracket that is no list of extras), and says in another way that
# the package is not one to ask PyPI about.
BROKEN_AND_EXPLAINED = {
    "pyproject.toml with a uv source": (
        "pyproject.toml",
        '[project]\nname = "demo"\ndependencies = ["corp-auth=1.0", "requests"]\n' + UV_SOURCE,
    ),
    "pyproject.toml with an ignore comment": (
        "pyproject.toml",
        f'[project]\nname = "demo"\ndependencies = [\n  "corp-auth ^1.0",{IGNORED}\n]\n',
    ),
    "requirements.txt with an ignore comment": (
        "requirements.txt",
        f"requests\ncorp-auth=1.0{IGNORED}\n",
    ),
    "requirements.txt with ignore-file": (
        "requirements.txt",
        "# slopfence: ignore-file\ncorp-auth ^1.0\n",
    ),
    "requirements.txt with a bracket": (
        "requirements.txt",
        f"requests\ncorp-auth [platform:dpkg]{IGNORED}\n",
    ),
}


@pytest.mark.parametrize("layout", sorted(BROKEN_AND_EXPLAINED))
def test_a_string_that_is_no_requirement_still_declares_its_name(
    write, check, not_installed, layout
):
    """``corp-auth=1.0`` is no requirement, so ``corp-auth`` is not looked up as a
    dependency. v0.2 took it for one: with an ignore comment or a uv source, the name
    stayed on the machine, and so did the import of the package. It still does."""
    write(*BROKEN_AND_EXPLAINED[layout])
    write("app.py", "import corp_auth\nimport ghost_mod_zz\n")
    registry = FakeRegistry({"requests"})
    result = check("SLOP001", registry=registry, check_imports=True)
    assert [f.subject for f in result.findings] == ["ghost_mod_zz"]
    assert not any("corp" in name for name in registry.queries)


def test_a_uv_source_for_a_string_that_is_no_requirement_counts_in_other_files(write, check):
    """The same without imports: the uv source says where ``corp-auth`` comes from, so
    the requirements file that names it again is not looked up for it. The broken
    string itself is never sent, with a source or without one."""
    broken = '[project]\nname = "demo"\ndependencies = ["corp-auth=1.0", "ghost-lib=2"]\n'
    write("pyproject.toml", broken + UV_SOURCE)
    write("requirements.txt", "corp-auth\nrequests\n")
    registry = FakeRegistry({"requests"})
    assert check("SLOP001", registry=registry).findings == []
    assert registry.queries == ["requests"]


def test_a_name_held_back_for_another_reason_is_still_looked_up_elsewhere(write, check):
    """Only an entry that says where its package comes from speaks for other files. A
    file-wide ``--find-links``, or an ignore comment, covers its own line and no more."""
    write("requirements.txt", "--find-links ./wheels\nghost-one\n")
    write("requirements-dev.txt", "ghost-two  # slopfence: ignore\n")
    write("setup.cfg", "[options]\ninstall_requires =\n    ghost-one\n    ghost-two\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert found(result) == [("setup.cfg", 3, 1), ("setup.cfg", 4, 1)]
    assert sorted(registry.queries) == ["ghost-one", "ghost-two"]


@pytest.mark.parametrize(
    "body",
    [
        "tool = 3\n",
        'project = "demo"\n',
        "[project]\nname = 3\ndependencies = 5\n",
        "[project]\noptional-dependencies = 3\n",
        "[project.optional-dependencies]\ndev = 3\n",
        "[dependency-groups]\ndev = 3\n",
        "[tool]\npoetry = 3\nuv = 4\n",
        "[tool.poetry]\nname = 3\ndependencies = 3\ngroup = 3\n",
        "[tool.poetry.group]\ndev = 3\n",
        "[tool.uv]\nsources = 3\n",
    ],
)
def test_a_pyproject_with_another_shape_does_not_stop_the_run(write, check, body):
    """Every ``pyproject.toml`` of the project is read, also a test fixture in an
    excluded folder. One with the wrong shape is read as far as it goes."""
    write("tests/fixtures/broken/pyproject.toml", body)
    write("requirements.txt", "ghost-lib\n")
    result = check("SLOP001", registry=FakeRegistry(set()), exclude=["tests/fixtures/"])
    assert found(result) == [("requirements.txt", 1, 1)]


DEEP = "x = " + "[" * 5000 + "]" * 5000 + "\n"


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("pyproject.toml", DEEP),
        ("pyproject.toml", "x = " + "9" * 10_000 + "\n"),
        ("Pipfile", DEEP),
        ("uv.lock", DEEP),
        ("uv.lock", "package = 3\n"),
        ("Pipfile.lock", "[" * 100_000),
        ("Pipfile.lock", '{"default": 3, "_meta": {"sources": 4}}'),
    ],
    ids=[
        "nested pyproject.toml",
        "huge number",
        "nested Pipfile",
        "nested uv.lock",
        "uv.lock with another shape",
        "nested JSON",
        "Pipfile.lock with another shape",
    ],
)
def test_a_file_the_toml_or_json_reader_gives_up_on_does_not_stop_the_run(write, check, name, text):
    """A generated file with thousands of nested brackets, or a number of ten thousand
    digits, is not one the reader takes. It is skipped, wherever in the project it is."""
    write(f"tests/fixtures/odd/{name}", text)
    write("requirements.txt", "ghost-lib\n")
    result = check("SLOP001", registry=FakeRegistry(set()), check_imports=True)
    assert found(result) == [("requirements.txt", 1, 1)]


# --- --diff, exclude, baseline, SARIF ---------------------------------------------------


def test_diff_and_exclude(write, check):
    """Findings follow --diff line by line, files outside the diff aren't looked up,
    and exclude patterns apply to the new files."""
    write("setup.cfg", "[options]\ninstall_requires =\n    ghost-old\n    ghost-new\n")
    write("Pipfile", "[packages]\nghost-pip = '*'\n")
    write("requirements.txt", "ghost-req\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, changed={"setup.cfg": {4}})
    assert found(result) == [("setup.cfg", 4, 1)]
    assert sorted(registry.queries) == ["ghost-new", "ghost-old"]
    result = check("SLOP001", registry=FakeRegistry(set()), exclude=["setup.cfg", "*.txt"])
    assert found(result) == [("Pipfile", 2, 1)]


def test_a_baseline_hides_findings_of_the_new_sources(tmp_path, monkeypatch, capsys):
    """A finding in setup.py or setup.cfg is about its package, so a baseline still
    matches it when the line moves."""
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\nsetup(install_requires=['Ghost_Lib'])\n", encoding="utf-8"
    )
    (tmp_path / "setup.cfg").write_text(
        "[options]\ninstall_requires =\n    ghost-cfg\n", encoding="utf-8"
    )
    cache = tmp_path / "cache" / "slopfence"
    cache.mkdir(parents=True)
    answers = {"ghost-lib": [False, 9e12], "ghost-cfg": [False, 9e12], "ghost-new": [False, 9e12]}
    (cache / "pypi.json").write_text(json.dumps(answers), encoding="utf-8")
    monkeypatch.setenv("SLOPFENCE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)
    assert main([".", "--write-baseline", "baseline.json"]) == 0
    capsys.readouterr()
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\n\n\n"
        "setup(\n    install_requires=['Ghost_Lib', 'ghost-new'],\n)\n",
        encoding="utf-8",
    )
    assert main([".", "--baseline", "baseline.json", "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert [(f["path"], f["line"]) for f in report["findings"]] == [("setup.py", 5)]
    assert "ghost-new" in report["findings"][0]["message"] and report["baselined"] == 2


def test_a_dependency_file_outside_the_project_is_still_checked(write, tmp_path):
    """``slopfence . ../shared/constraints.txt``: the file is not part of the project,
    and it is checked because it was named. So it is when single files are named."""
    own = write("app/requirements.txt", "requests\n")
    shared = write("shared/constraints.txt", "ghost-pin==1.0\n")
    for paths in ([tmp_path / "app", shared], [own, shared]):
        result = run(paths, tmp_path / "app", FakeRegistry({"requests"}))
        outside = shared.resolve().as_posix()  # reported with its full path
        assert [(f.path, f.subject) for f in result.findings] == [(outside, "ghost-pin")]
        assert result.files_checked == 2


# A named file that the walk of the project never reaches, with the folder that is its
# own checkout (None: it has none) and the requirements file that sets its index.
OFF_THE_WALK = {
    "a nested clone": ("proj/vendor/lib/src/setup.py", "proj/vendor/lib", "requirements.txt"),
    "deep in a clone": ("proj/vendor/lib/a/b/setup.py", "proj/vendor/lib", "constraints.txt"),
    "a nested worktree": ("proj/wt/fix/setup.py", "proj/wt/fix", "deploy/requirements.txt"),
    "another project": ("other/pkg/setup.py", "other", "requirements.txt"),
    "outside, no checkout": ("other/setup.py", None, "requirements.txt"),
    "a build folder": ("proj/build/lib/setup.py", None, "requirements.txt"),
    "a virtual environment": ("proj/.venv/src/tool/setup.cfg", None, "requirements-prod.txt"),
}
OFF_THE_WALK_BODY = {
    "setup.py": "from setuptools import setup\nsetup(name='x', install_requires=['corp-auth'])\n",
    "setup.cfg": "[options]\ninstall_requires =\n    corp-auth\n",
}


@pytest.mark.parametrize("layout", sorted(OFF_THE_WALK))
def test_a_named_file_off_the_project_walk_is_judged_with_its_own_checkout(write, tmp_path, layout):
    """``slopfence . vendor/lib/setup.py``: the walk of the project skips a nested
    checkout, a build folder and everything outside the project. A file that is
    named there is checked all the same, together with the dependency files of its
    own checkout, or of its own folder when it has none. So a private index that is
    set there covers it. It does the same when a folder is named: the checkout, or
    a folder inside it."""
    named, checkout, requirements = OFF_THE_WALK[layout]
    path = write(named, OFF_THE_WALK_BODY[PurePosixPath(named).name])
    home = tmp_path / checkout if checkout else path.parent
    index = write((home / requirements).relative_to(tmp_path).as_posix(), INDEX + "corp-core\n")
    if checkout and "worktree" in layout:
        write(f"{checkout}/.git", "gitdir: ../../.git/worktrees/fix\n")
    elif checkout:
        write(f"{checkout}/.git/HEAD", "ref: refs/heads/main\n")
    root = tmp_path / "proj"
    write("proj/app.py", "x = 1\n")
    for paths in ([root, path], [path], [root, home], [root, path.parent], [path.parent]):
        registry = FakeRegistry(set())
        result = run(paths, root, registry, select=["SLOP001"])
        assert result.findings == [] and registry.queries == []
        assert f"a private package index is set in {relative_path(index, root)}," in result.notes[0]
        assert "secret" not in result.notes[0]


GHOST_CFG = "[options]\ninstall_requires =\n    ghost-lib\n"


# Files that are nobody's project for a file named in ``loose/shared``.
NOT_BESIDE = {
    "an index above": ("loose/requirements.txt", INDEX + "corp-core\n"),
    "an index far below": ("loose/shared/old/app/requirements.txt", INDEX + "corp-core\n"),
    "a broken file far below": ("loose/shared/old/app/pyproject.toml", "not TOML\n"),
}


@pytest.mark.parametrize("layout", sorted(NOT_BESIDE))
@pytest.mark.parametrize("named", ["setup.cfg", "notes.md"])
def test_a_named_file_without_a_checkout_is_judged_with_its_own_folder_only(
    write, tmp_path, named, layout
):
    """``slopfence . ../loose/shared/setup.cfg``, where no folder above the file holds
    a ``.git``: only the files that lie directly in its own folder count. The folder
    above is not read, and the folders below are nobody's project. A private index or
    a broken file there holds nothing back: not the named file, and not the project."""
    write("proj/setup.cfg", GHOST_CFG)
    write("loose/shared/setup.cfg", GHOST_CFG.replace("ghost-lib", "ghost-shared"))
    path = write("loose/shared/notes.md", "notes\n").with_name(named)
    write(*NOT_BESIDE[layout])
    root = tmp_path / "proj"
    registry = FakeRegistry(set())
    result = run([root, path], root, registry, select=["SLOP001"])
    asked = ["ghost-lib", "ghost-shared"] if named == "setup.cfg" else ["ghost-lib"]
    assert sorted(registry.queries) == asked and result.notes == []


def test_a_named_folder_without_a_checkout_is_not_judged_with_the_folder_above(write, tmp_path):
    """The same for ``slopfence . ../loose/shared``: the named folder is read as far
    down as it goes, and nothing above it is. So the file and its folder agree."""
    write("proj/setup.cfg", GHOST_CFG)
    path = write("loose/shared/setup.cfg", GHOST_CFG.replace("ghost-lib", "ghost-shared"))
    write("loose/requirements.txt", INDEX + "corp-core\n")
    root = tmp_path / "proj"
    for named in (path, path.parent):
        registry = FakeRegistry(set())
        result = run([root, named], root, registry, select=["SLOP001"])
        assert sorted(registry.queries) == ["ghost-lib", "ghost-shared"] and result.notes == []


def test_a_folder_that_can_not_be_entered_does_not_stop_the_run(write, tmp_path):
    """A folder without permissions lies next to a named file outside the project, and
    another inside the project. Whether it is a checkout can't be asked, and before
    Python 3.13 asking raised. It is passed by, like a folder that is none."""
    write("proj/setup.cfg", GHOST_CFG)
    shared = write("other/sub/constraints.txt", "requests==2.31\n")
    write("other/.git/HEAD", "ref: refs/heads/main\n")
    locked = [tmp_path / "proj" / "locked", tmp_path / "other" / "locked"]
    for folder in locked:
        folder.mkdir()
        folder.chmod(0)
    try:
        root = tmp_path / "proj"
        registry = FakeRegistry({"requests"})
        result = run([root, shared], root, registry, select=["SLOP001"])
    finally:
        for folder in locked:
            folder.chmod(0o755)
    assert [f.subject for f in result.findings] == ["ghost-lib"]


def test_the_folders_above_the_project_root_are_never_read(write, tmp_path):
    """The look-alike of a nested checkout: the project is a folder of a bigger
    checkout, and the root or a folder of the project is named. The requirements file
    above the root is not this project's, as it never was."""
    write("outer/.git/HEAD", "ref: refs/heads/main\n")
    write("outer/requirements.txt", INDEX + "corp-core\n")
    write("outer/proj/setup.cfg", GHOST_CFG)
    write("outer/proj/sub/setup.cfg", GHOST_CFG.replace("ghost-lib", "ghost-sub"))
    root = tmp_path / "outer" / "proj"
    for paths, names in (([root], ["ghost-lib", "ghost-sub"]), ([root / "sub"], ["ghost-sub"])):
        registry = FakeRegistry(set())
        result = run(paths, root, registry, select=["SLOP001"])
        assert registry.queries == names and result.notes == []


def test_a_named_file_the_walk_reaches_is_judged_with_the_project_alone(write, tmp_path):
    """The look-alike: a file of the project itself is named. A nested checkout is
    still another project, so its private index covers nothing here."""
    path = write("pkg/setup.cfg", "[options]\ninstall_requires =\n    ghost-lib\n")
    write("vendor/lib/.git/HEAD", "ref: refs/heads/main\n")
    write("vendor/lib/requirements.txt", INDEX + "corp-core\n")
    registry = FakeRegistry(set())
    result = run([path], tmp_path, registry, select=["SLOP001"])
    assert found(result) == [("pkg/setup.cfg", 3, 1)] and registry.queries == ["ghost-lib"]
    assert result.notes == []


def test_sarif_location_of_a_setup_py_finding(tmp_path, monkeypatch, capsys):
    """SARIF carries the real line and column."""
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\n\nsetup(install_requires=['requests', 'ghost-lib'])\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache" / "slopfence"
    cache.mkdir(parents=True)
    (cache / "pypi.json").write_text(
        json.dumps({"requests": [True, 9e12], "ghost-lib": [False, 9e12]}), encoding="utf-8"
    )
    monkeypatch.setenv("SLOPFENCE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)
    assert main([".", "--format", "sarif"]) == 1
    [res] = json.loads(capsys.readouterr().out)["runs"][0]["results"]
    location = res["locations"][0]["physicalLocation"]
    assert location["artifactLocation"]["uri"] == "setup.py"
    assert location["region"] == {"startLine": 3, "startColumn": 37}
