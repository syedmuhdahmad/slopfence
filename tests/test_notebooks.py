"""SLOP001 in Jupyter notebooks: pip install lines and imports (#53)."""

import contextlib
import json

import pytest

from slopfence import diff as diffmod
from slopfence.cli import main
from slopfence.detectors import notebooks, packages
from slopfence.detectors.notebooks import read_notebook, scan_notebook
from slopfence.detectors.pip_commands import pip_install_arguments, pip_requirements, shell_commands
from slopfence.engine import discover_all, run
from slopfence.registry import OfflineRegistry, PyPIRegistry
from tests.conftest import FakeRegistry, git, git_init

PRIVATE = "https://user:secret@pkgs.corp.example/simple"


def notebook(cells, **top):
    """A notebook dict with one markdown cell and the given code cells."""
    body = [{"cell_type": "markdown", "metadata": {}, "source": ["# Title\n", "%pip install md"]}]
    for n, source in enumerate(cells):
        body.append(
            {
                "cell_type": "code",
                "execution_count": n,
                "metadata": {},
                # The output repeats the source: text outside "source" must never count.
                "outputs": [{"output_type": "stream", "name": "stdout", "text": [source]}],
                "source": source.splitlines(keepends=True),
            }
        )
    return {
        "cells": body,
        "metadata": {"kernelspec": {"language": "python", "name": "python3"}},
        "nbformat": 4,
        "nbformat_minor": 5,
        **top,
    }


def jupyter(cells, **top):
    """The text Jupyter itself writes: indent 1, sorted keys, one source line per line."""
    return json.dumps(notebook(cells, **top), indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def found(result):
    """(path, line, column) of every finding."""
    return [(f.path, f.line, f.col) for f in result.findings]


def subjects(result):
    """What each finding is about, in report order."""
    return [f.subject for f in result.findings]


# --- pip command lines ------------------------------------------------------------------


def pip(line):
    """What the pip install commands on a line say: the names that may be looked up,
    the names that may not, whether a private index is named, and whether links are.
    "not pip" when no command is ``pip install``."""
    commands, _ = shell_commands(line, 0)
    public, held_back, index, links, seen = [], [], False, False, False
    for words in commands:
        arguments = pip_install_arguments(words)
        if arguments is None:
            continue
        seen = True
        install = pip_requirements(arguments)
        for name, _, sourced in install.packages:
            (held_back if sourced or install.variables else public).append(name)
        index, links = index or install.private_index, links or install.links
    return (public, held_back, index, links) if seen else "not pip"


@pytest.mark.parametrize(
    ("line", "names"),
    [
        ("pip install requests", ["requests"]),
        (
            "pip3 install -q -U 'requests>=2' \"flask[async]==2.*\" ghost_lib",
            ["requests", "flask", "ghost_lib"],
        ),
        ("pip3.12 install ghost", ["ghost"]),
        ("python -m pip install --upgrade pip wheel", ["pip", "wheel"]),
        ("{sys.executable} -m pip install numpy", ["numpy"]),
        ("uv pip install --system polars", ["polars"]),
        ("pip install -i https://pypi.org/simple requests", ["requests"]),
        (
            "pip install --index-url=https://pypi.org/simple/"
            " --extra-index-url https://pypi.org/simple x",
            ["x"],
        ),
        ("pip install foo > /dev/null 2>&1", ["foo"]),
        ("pip install transformers>=4.19", ["transformers"]),  # the shell sees a redirection
        ("pip install foo && pip install bar; echo done", ["foo", "bar"]),
        ("apt-get install -y libpq-dev && pip install psycopg2", ["psycopg2"]),
        ("pip install foo  # a comment", ["foo"]),
        ("pip install -qqq -Uq foo", ["foo"]),
        ("pip install -rrequirements.txt foo", ["foo"]),
        ("pip install --target ./lib --no-deps foo", ["foo"]),
        ("pip install foo \\\n    bar", ["foo", "bar"]),
        ("pip install foo\\\nbar", ["foobar"]),  # the shell joins the two lines
        ("pip install discord.py ruamel.yaml", ["discord.py", "ruamel.yaml"]),
        ("pip install -r requirements.txt -c constraints.txt -e . ./local /abs/x.whl", []),
        ("pip install requirements.txt mypkg.whl C:\\\\tmp\\\\x.whl ~/pkg", []),
        ("pip install src\\mypkg \"lib\\other\" 'raw\\path'", []),  # Windows paths
        ("pip install https://example.com/y.whl", []),
        ("pip install -- -weird-name", []),
        ("pip install python==3.11", []),
        ("pip install", []),
    ],
)  # fmt: skip
def test_pip_install_lines(line, names):
    """Package names are found; options, files, paths and URLs are not names."""
    assert pip(line) == (names, [], False, False)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # A URL that names its package: declared, never looked up.
        ("pip install git+https://x.example/r.git@v1#egg=repo", ([], ["repo"], False, False)),
        ("pip install 'corp @ https://example.com/corp.whl'", ([], ["corp"], False, False)),
        # An index that is not PyPI.
        ("pip install --index-url https://corp.example/simple corp", (["corp"], [], True, False)),
        ("pip install --extra-index-url=https://c.example/w torch", (["torch"], [], True, False)),
        ("uv pip install --index corp=https://c.example/simple corp", (["corp"], [], True, False)),
        ("pip install -ihttps://corp.example/simple corp", (["corp"], [], True, False)),
        ("pip install corp -i", (["corp"], [], True, False)),
        # TestPyPI is another index than PyPI.
        (
            "pip install -i https://test.pypi.org/simple/ --no-deps example-pkg",
            (["example-pkg"], [], True, False),
        ),
        # Links, and no index at all.
        ("pip install torch -f https://corp.example/whl/torch.html", (["torch"], [], False, True)),
        ("pip install --find-links=./wheels torch", (["torch"], [], False, True)),
        ("pip install --no-index corp", (["corp"], [], False, True)),
        # A variable may hold anything, an index option too: nothing is looked up.
        (
            "pip install $PACKAGE {pkg} foo=={version} bar==$VER `cmd`",
            ([], ["foo", "bar"], False, False),
        ),
        ("pip install $OPTS corp-auth", ([], ["corp-auth"], False, False)),
        ("pip install %PIP_OPTS% corp-auth", ([], ["corp-auth"], False, False)),  # Windows
        ("pip install corp-auth --index-url $index", ([], ["corp-auth"], True, False)),
        ('pip install "pkg\\$x" other', ([], ["other"], False, False)),
        # An option this version doesn't know could take a value: no word of the
        # command is taken for a package.
        ("pip install --some-new-option value foo", ([], [], False, False)),
        ("pip install -x foo", ([], [], False, False)),
        ("pip install -qx foo", ([], [], False, False)),
        # What such a command says about an index still counts, whatever the value is.
        ("pip install --new-flag -i https://corp.example/simple corp", ([], [], True, False)),
        ("pip install --new-flag -ihttps://corp.example/simple corp", ([], [], True, False)),
        ("uv pip install --new-flag --index https://c.example/s corp", ([], [], True, False)),
        ("pip install --new-flag --index-url=https://pypi.org/simple x", ([], [], True, False)),
        ("pip install --new-flag --find-links=./wheels x", ([], [], False, True)),
        ("pip install -x -f ./wheels x", ([], [], False, True)),
        ("pip install 'unterminated", "not pip"),
        ("pip download foo", "not pip"),
        ("pip --quiet install foo", "not pip"),
        ("conda install numpy", "not pip"),
        ("python -m venv .venv", "not pip"),
    ],
)  # fmt: skip
def test_pip_install_lines_special_cases(line, expected):
    """URLs are declared but never looked up, index and link options are noticed, a
    variable holds every name back, and anything unclear is skipped."""
    assert pip(line) == expected


@pytest.mark.parametrize(
    ("line", "unnamed"),
    [
        ("pip install requests 'flask[async]==2.*'", False),
        ("pip install -q -U --no-deps requests -c constraints.txt", False),
        ("pip install git+https://x.example/r.git#egg=repo", False),
        ("pip install -i https://corp.example/simple corp", False),
        # A URL or a path that does not say what it installs.
        ("pip install git+https://github.com/x/detectron_zz.git", True),
        ("pip install https://example.com/y.whl requests", True),
        ("pip install .", True),
        ("pip install ./dist/x.whl", True),
        # Options that install what another file or folder lists.
        ("pip install -r requirements.txt", True),
        ("pip install -rrequirements.txt foo", True),
        ("pip install --editable=.", True),
        ("pip install -e git+https://x.example/r.git#egg=repo", True),
        ("uv pip install --group dev", True),
        # A variable, and a command that was not read.
        ("pip install $PKG", True),
        ("pip install --some-new-flag pkg", True),
    ],
)
def test_commands_that_install_something_without_a_name(line, unnamed):
    """Whether a command installs something that it gives no name for here. What
    such a command brings in can't be told from an import that nothing provides."""
    [words] = shell_commands(line, 0)[0]
    assert pip_requirements(pip_install_arguments(words)).unnamed is unnamed


def test_shell_words_know_where_they_are():
    """Each word carries where it starts and ends in the line, quotes included."""
    line = "pip install 'a b' c"
    commands, end = shell_commands(line, 0)
    assert [(w.text, line[w.start : w.end]) for w in commands[0]] == [
        ("pip", "pip"),
        ("install", "install"),
        ("a b", "'a b'"),
        ("c", "c"),
    ]
    assert end == len(line)


# --- Notebooks through the engine ---------------------------------------------------------

CELLS = [
    "%pip install requests ghost-nb-lib\nimport requests\n",
    "import os, sys\nif 'google.colab' in sys.modules:\n"
    # Text that is not ASCII, and a backslash, in front of the last name.
    "    !pip install -q \"ghost-one; extra == '\u00f6'\" 'ghost-tab; extra == \"a\\tb\"'"
    " ghost-two==1.0\n",
    "%%bash\npip install ghost-three \\\n    ghost-four\necho done\n",
    "%%html\n<b>!pip install not-this</b>\n",
    'text = """\n!pip install in-a-string\n"""\n'
    "from ghost_import import x  # caf\u00e9 \U0001f600\n",
    "# !pip install commented-out\n%%capture\n",
    "%matplotlib inline\nfiles = !ls\nimport numpy as np\nnp.array?\n",
    "pip install automagic-pkg\n",
    "this is not python (\n!pip install still-found\n",
    "%%capture\n!python -m pip install ghost-five\n%pip install ghost-six\n",
    "!conda install conda-pkg\n!apt-get install -y apt-pkg\n%pip download dl-pkg\n",
    "%load_ext autoreload\nhelp_text = '''\npip install in-a-string-too\n'''\n",
]
NAMES = [
    "requests", "ghost-nb-lib", "ghost-one", "ghost-tab", "ghost-two", "ghost-three",
    "ghost-four", "automagic-pkg", "still-found", "ghost-five", "ghost-six",
]  # fmt: skip
IMPORTS = ["requests", "os", "sys", "ghost_import", "numpy"]


def layouts():
    """The same notebook written the ways tools write it."""
    nb = notebook(CELLS)
    as_string = notebook(CELLS)
    for cell in as_string["cells"]:
        cell["source"] = "".join(cell["source"])
    reordered = {**nb, "cells": [dict(reversed(list(c.items()))) for c in nb["cells"]]}
    return {
        "jupyter": jupyter(CELLS),
        "indent 2": json.dumps(nb, indent=2, ensure_ascii=False),
        "indent 4, ascii escapes": json.dumps(nb, indent=4, ensure_ascii=True),
        "tabs": json.dumps(nb, indent="\t", ensure_ascii=False),
        "minified": json.dumps(nb, separators=(",", ":"), ensure_ascii=False),
        "minified, ascii escapes": json.dumps(nb, separators=(",", ":")),
        "one line": json.dumps(nb),
        "source before outputs": json.dumps(reordered, indent=1),
        "source as one string": json.dumps(as_string, indent=1, ensure_ascii=False),
        "source as one string, minified": json.dumps(as_string, separators=(",", ":")),
    }


@pytest.mark.parametrize("layout", sorted(layouts()))
def test_positions_point_into_the_ipynb_file(layout):
    """Whatever the formatting, each line and column lands on the name in the file."""
    text = layouts()[layout]
    declared = scan_notebook(text, "nb.ipynb", want_imports=True)
    assert [d.name for d in declared.dependencies] == NAMES
    assert [name for name, _, _ in declared.imports] == IMPORTS
    lines = text.split("\n")
    for dep in declared.dependencies:
        assert lines[dep.line - 1][dep.col - 1 :].startswith(dep.name), dep
    for name, line, col in declared.imports:
        assert lines[line - 1][col - 1 :].startswith(("import ", "from ")), name
    assert all(d.validate and not d.suppressed for d in declared.dependencies)
    assert not declared.private_index


def test_jupyter_layout_has_one_source_line_per_file_line():
    """In Jupyter's own layout the finding is on the line git shows for that source line."""
    text = jupyter(CELLS)
    declared = scan_notebook(text, "nb.ipynb").dependencies
    dep = next(d for d in declared if d.name == "ghost-nb-lib")
    assert text.split("\n")[dep.line - 1].strip() == '"%pip install requests ghost-nb-lib\\n",'
    assert dep.col == text.split("\n")[dep.line - 1].index("ghost-nb-lib") + 1


def test_notebook_found_and_missing(write, check):
    """A missing package in a pip line is reported at its place in the .ipynb file."""
    text = jupyter(
        [
            "%pip install requests ghost-nb-lib\n",
            "!pip install 'corp-git @ git+https://x.example/c' Ghost_Other\n",
        ]
    )
    write("nb/demo.ipynb", text)
    write("nb/.ipynb_checkpoints/demo-checkpoint.ipynb", text)
    registry = FakeRegistry({"requests"})
    result = check("SLOP001", registry=registry)
    # The cell's output repeats the text first; the finding is on the "source" line.
    line = max(n for n, row in enumerate(text.split("\n"), 1) if "ghost-nb-lib" in row)
    col = text.split("\n")[line - 1].index("ghost-nb-lib") + 1
    assert found(result)[0] == ("nb/demo.ipynb", line, col)
    assert subjects(result) == ["ghost-nb-lib", "ghost-other"]
    assert result.findings[0].message == (
        "Dependency 'ghost-nb-lib' does not exist on PyPI (possible hallucination)"
    )
    assert "corp-git" not in registry.queries  # from a URL: never sent
    # Jupyter's autosaved copy is not a second notebook.
    assert result.files_checked == 1 and result.parse_errors == []


def test_checkpoint_folders_are_never_walked(write, tmp_path):
    """``.ipynb_checkpoints`` holds copies that Jupyter saves by itself."""
    write("nb/a.ipynb", "{}")
    write("nb/.ipynb_checkpoints/a-checkpoint.ipynb", "{}")
    assert [p.name for p in discover_all([tmp_path], tmp_path)[1]] == ["a.ipynb"]


@pytest.mark.parametrize(
    "cell",
    [
        f"%pip install --index-url {PRIVATE} corp-a\n",
        "%pip install -i https://test.pypi.org/simple/ corp-a\n",
        "%pip install --no-index corp-a\n",
        "!pip install corp-a -f https://corp.example/wheels.html\n",
        # A command with an option that is not known: its index still counts.
        f"!pip install --brand-new-flag -i {PRIVATE} corp-a\n",
        f"!uv pip install --brand-new-flag --index {PRIVATE} corp-a\n",
        "!pip install --brand-new-flag -f ./wheels corp-a\n",
        # A long option cut short, as pip allows.
        f"!pip install --extra-index {PRIVATE} corp-a\n",
        f"!pip install --extra-index={PRIVATE} corp-a\n",
        f"!pip install --pre --ind {PRIVATE} corp-a\n",
        "!pip install --find ./wheels corp-a\n",
        "!pip install --no-ind corp-a\n",
    ],
)
def test_a_private_source_covers_the_whole_notebook(write, check, cell):
    """An index or links option in one pip line: every package of the notebook may
    come from there, and so may its imports."""
    write("nb/demo.ipynb", jupyter([cell, "!pip install corp-b\nimport corp_c\n"]))
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, check_imports=True)
    assert result.findings == [] and registry.queries == []


@pytest.mark.parametrize(
    "option", ["--no-input", "--no-inp", "--exists-action=w", "--force", "--isolated"]
)
def test_an_option_that_only_starts_like_a_source_option_is_none(write, check, option):
    """``--no-inp`` is short for ``--no-input``, not for ``--no-index``: the notebook
    names no other source, so its other pip lines are still looked up."""
    write("nb/demo.ipynb", jupyter([f"!pip install {option} pandas\n", "!pip install ghost-b\n"]))
    result = check("SLOP001", registry=FakeRegistry({"pandas"}))
    assert subjects(result) == ["ghost-b"]


def test_links_in_a_notebook_only_cover_that_notebook(write, check, not_installed):
    """``--find-links`` and ``--no-index`` name no index: other files are still checked."""
    write("a.ipynb", jupyter(["!pip install torch -f https://corp.example/wheels.html\n"]))
    write("b.ipynb", jupyter(["%pip install ghost-b\nimport ghost_mod_b\n"]))
    write("app.py", "import ghost_mod_app\n")
    result = check("SLOP001", registry=FakeRegistry(set()), check_imports=True)
    assert subjects(result) == ["ghost_mod_app", "ghost-b", "ghost_mod_b"]
    assert result.notes == []


def test_an_index_in_a_notebook_stops_import_lookups_everywhere(write, check, not_installed):
    """With ``check_imports``, a notebook that installs from a private index says that
    unknown imports may be private: in other notebooks, and in source code."""
    write("nb/setup.ipynb", jupyter([f"%pip install --extra-index-url {PRIVATE} corp-auth\n"]))
    write("nb/analysis.ipynb", jupyter(["import corp_core\n%pip install ghost-lib\n"]))
    write("src/app.py", "import corp_models\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, check_imports=True)
    # The other notebook's declared package is still looked up: it names no index.
    assert subjects(result) == ["ghost-lib"] and registry.queries == ["ghost-lib"]
    assert result.notes == [
        "SLOP001 didn't look up imports on PyPI: nb/setup.ipynb uses a private package "
        "index, so unknown imports may be private packages."
    ]
    assert "secret" not in result.notes[0]


@pytest.mark.parametrize(
    "cells",
    [
        ["!pip config set global.index-url https://corp.example/simple", "!pip install corp-auth"],
        ["%env PIP_INDEX_URL=https://corp.example/simple", "%pip install corp-auth"],
        [
            "import os\nos.environ['PIP_EXTRA_INDEX_URL'] = 'https://c.example/s'",
            "!pip install corp-auth",
        ],
        ["!aws codeartifact login --tool pip --domain corp", "%pip install corp-auth"],
        ["!gcloud artifacts print-settings python  # artifactregistry", "%pip install corp-auth"],
        ["!PIP_INDEX_URL=https://corp.example/simple pip install corp-auth"],
        ["%%bash\nOPTS='--index-url https://corp.example/simple'\npip install $OPTS corp-auth"],
        ["index = 'https://corp.example/simple'\n!pip install corp-auth --index-url $index"],
        [
            "%%writefile ~/.pip/pip.conf\n[global]\nindex-url = https://corp.example/simple",
            "!pip install corp-auth",
        ],
        ["%%bash\nexport UV_INDEX=https://corp.example/simple\nuv pip install corp-auth"],
        ["%env UV_DEFAULT_INDEX=https://corp.example/simple", "!uv pip install corp-auth"],
        ["import keyring", "%pip install corp-auth"],
        ["%env PIP_FIND_LINKS=/mnt/wheels", "%pip install corp-auth"],
        ["# pip.ini on the cluster sets the index", "%pip install corp-auth"],
        ["%env PIP_CONFIG_FILE=/dbfs/FileStore/pip.cfg", "%pip install corp-auth"],
        # In a command that was read: as a package, and as another option.
        ["!pip install keyring keyrings.google-artifactregistry-auth", "!pip install corp-auth"],
        ["!pip install keyring keyrings.google-artifactregistry-auth corp-auth"],
        ["%pip install keyrings.codeartifact", "%pip install corp-auth"],
        ["!pip install --keyring-provider subprocess corp-auth"],
        ["!uv pip install --keyring-provider subprocess corp-auth"],
    ],
)  # fmt: skip
def test_an_index_set_outside_the_install_line_makes_the_notebook_private(write, check, cells):
    """A company notebook sets its index with ``pip config``, an environment variable,
    a ``pip.conf`` or a login command. Any cell that mentions such a setting means the
    notebook's names stay on the machine, and its imports too."""
    write("nb.ipynb", jupyter([*cells, "import corp_core\n"]))
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, check_imports=True)
    assert result.findings == [] and registry.queries == []


def test_an_index_option_that_names_pypi_is_not_a_private_setting(write, check):
    """``--index-url https://pypi.org/simple`` in a pip line that was read is public.
    The same words outside a command that was read are not trusted."""
    write("a.ipynb", jupyter(["%pip install --index-url https://pypi.org/simple ghost-a\n"]))
    write(
        "b.ipynb",
        jupyter(["%pip install --brand-new-flag --index-url https://pypi.org/simple ghost-b\n"]),
    )
    registry = FakeRegistry(set())
    assert subjects(check("SLOP001", registry=registry)) == ["ghost-a"]
    assert registry.queries == ["ghost-a"]
    # Links in a command that was read cover the notebook's packages, and name no index.
    write("a.ipynb", jupyter(["%pip install --find-links=./wheels corp-a\n"]))
    write("b.ipynb", jupyter(["%pip install ghost-c\n"]))
    write("app.py", "import ghost_mod\n")
    result = check("SLOP001", registry=FakeRegistry(set()), check_imports=True)
    assert subjects(result) == ["ghost_mod", "ghost-c"] and result.notes == []


def test_notebook_inherits_a_private_index_from_the_project(write, check, tmp_path):
    """Like setup.py: a private index in a project file covers notebooks, also when
    the notebook is the only file that is checked."""
    nb = write("demo.ipynb", jupyter(["%pip install corp-auth\n"]))
    write("requirements.txt", f"--index-url {PRIVATE}\ncorp-auth\n")
    for paths in ([tmp_path], [nb]):
        registry = FakeRegistry(set())
        result = run(paths, tmp_path, registry, select=["SLOP001"])
        assert result.findings == [] and registry.queries == []
        assert result.notes == [
            "SLOP001 didn't look up the dependencies in demo.ipynb: a private package index "
            "is set in requirements.txt, so names missing from PyPI may be private packages. "
            "If that index only mirrors PyPI, set detect-private-index = false."
        ]
    registry = FakeRegistry(set())
    result = run([tmp_path], tmp_path, registry, select=["SLOP001"], inherit_index=False)
    assert subjects(result) == ["corp-auth"]


def test_a_package_with_a_source_of_its_own_is_not_looked_up_in_a_notebook(write, check):
    """A package that a project file installs from git is the same package in a
    notebook. And one that a notebook installs from a URL is not looked up elsewhere."""
    write("requirements.txt", "corp-auth @ git+https://git.corp.example/corp-auth.git\n")
    write("demo.ipynb", jupyter(["%pip install corp-auth ghost-lib\n"]))
    registry = FakeRegistry(set())
    assert subjects(check("SLOP001", registry=registry)) == ["ghost-lib"]
    assert registry.queries == ["ghost-lib"]

    write("requirements.txt", "corp-kit\n")
    write("demo.ipynb", jupyter(["!pip install git+https://git.corp.example/k.git#egg=corp-kit\n"]))
    registry = FakeRegistry(set())
    assert check("SLOP001", registry=registry).findings == [] and registry.queries == []
    # A variable holds the names of its own command back, and no other.
    write("demo.ipynb", jupyter(["!pip install $OPTS corp-kit\n"]))
    registry = FakeRegistry(set())
    assert found(check("SLOP001", registry=registry)) == [("requirements.txt", 1, 1)]
    assert registry.queries == ["corp-kit"]


def test_names_that_never_leave_the_machine(write, check):
    """Known packages, the project's own packages, a notebook where SLOP001 is
    switched off, and every name of an offline run."""
    write("pkg/setup.py", "from setuptools import setup\nsetup(name='acme-core')\n")
    write("demo.ipynb", jupyter(["%pip install acme-core corp-known ghost-lib\n"]))
    write("off.ipynb", jupyter(["%pip install ghost-off\nimport ghost_off\n"]))
    registry = FakeRegistry(set())
    result = check(
        "SLOP001",
        registry=registry,
        known_packages=["corp-*"],
        per_file_ignores={"off.ipynb": ["SLOP001"]},
        check_imports=True,
    )
    assert subjects(result) == ["ghost-lib"] and registry.queries == ["ghost-lib"]
    assert check("SLOP001", registry=OfflineRegistry(), check_imports=True).findings == []


def test_ignore_comments_in_notebooks(write, check):
    """On the pip line, on a comment line right above it, or ignore-file in a code cell."""
    write(
        "demo.ipynb",
        jupyter(
            [
                "# slopfence: ignore\n!pip install ignored-above\n"
                "!pip install ignored-inline  # slopfence: ignore[SLOP001]\n"
                "!pip install other-rule  # slopfence: ignore[SLOP051]\n",
                "x = 1  # slopfence: ignore\n!pip install not-covered\n",
                "import ghost_a  # slopfence: ignore\nimport ghost_b\n",
            ]
        ),
    )
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, check_imports=True)
    assert subjects(result) == ["other-rule", "not-covered", "ghost_b"]
    assert not {"ignored-above", "ignored-inline", "ghost_a"} & set(registry.queries)

    write(
        "demo.ipynb", jupyter(["# slopfence: ignore-file\n", "!pip install ghost\nimport ghost_m"])
    )
    registry = FakeRegistry(set())
    assert check("SLOP001", registry=registry, check_imports=True).findings == []
    assert registry.queries == []
    # In a markdown cell it is just text.
    nb = notebook(["!pip install ghost\n"])
    nb["cells"][0]["source"] = ["# slopfence: ignore-file"]
    write("demo.ipynb", json.dumps(nb, indent=1))
    assert len(check("SLOP001", registry=FakeRegistry(set())).findings) == 1


@pytest.mark.parametrize(
    "cells",
    [
        ["%%bash\n# slopfence: ignore-file\npip install ghost\n", "import ghost_m\n"],
        ["%%bash\n# slopfence: ignore-file\necho hi\n", "!pip install ghost\nimport ghost_m\n"],
        ["# slopfence: ignore-file\n!pip install ghost\nfor x in:\n", "import ghost_m\n"],
        ["!pip install ghost  # slopfence: ignore-file\n", "%pip install ghost-two\n"],
        ["%%writefile notes.txt\n# slopfence: ignore-file\n", "!pip install ghost\n"],
        ["%%capture\n# Slopfence: Ignore-File\n!pip install ghost\n"],
    ],
    ids=["shell cell", "another shell cell", "not Python", "after a command", "writefile", "case"],
)
def test_ignore_file_counts_in_every_code_cell(write, check, cells):
    """``# slopfence: ignore-file`` skips the notebook wherever a code cell holds it:
    in a shell cell, in a cell that is not Python, or at the end of a command line.
    Nothing of the notebook is sent."""
    write("demo.ipynb", jupyter(cells))
    registry = FakeRegistry(set())
    assert check("SLOP001", registry=registry, check_imports=True).findings == []
    assert registry.queries == []


def test_an_ignore_comment_ends_a_command_that_goes_on_over_several_lines(write, check):
    """The comment of a continued command is at the end of its last line. It covers
    the whole command, and no other."""
    write(
        "demo.ipynb",
        jupyter(
            [
                "!pip install ghost-a \\\n    ghost-b  # slopfence: ignore\n!pip install ghost-c\n",
                "%%bash\npip install ghost-d \\\n  ghost-e # slopfence: ignore[SLOP001]\n",
                "!pip install ghost-f \\\n    ghost-g\n# slopfence: ignore\nx = 1\n",
            ]
        ),
    )
    registry = FakeRegistry(set())
    assert subjects(check("SLOP001", registry=registry)) == ["ghost-c", "ghost-f", "ghost-g"]
    assert sorted(registry.queries) == ["ghost-c", "ghost-f", "ghost-g"]


def test_notebook_imports_need_check_imports(write, check, not_installed):
    """Import names in cells are only sent with check_imports, and cells that aren't
    Python (magics, shell lines, broken code) are never a parse error."""
    write("helper.py", "")
    text = jupyter(
        [
            "%pip install declared-pkg\n%matplotlib inline\nimport declared_pkg, helper, os\n",
            "files = !ls\nimport ghost_mod\nfrom ghost_mod.sub import x\n",
            "%%writefile x.py\nimport not_this\n",
            "import never_parsed (\n",
            "%%time\nimport ghost_timed\n",
        ]
    )
    write("nb.ipynb", text)
    registry = FakeRegistry({"declared-pkg"})
    result = check("SLOP001", registry=registry)
    assert result.findings == [] and registry.queries == ["declared-pkg"]

    registry = FakeRegistry({"declared-pkg"})
    result = check("SLOP001", registry=registry, check_imports=True)
    assert subjects(result) == ["ghost_mod", "ghost_mod", "ghost_timed"]
    lines = text.split("\n")
    for f in result.findings:
        assert lines[f.line - 1][f.col - 1 :].startswith(("import ", "from "))
    assert result.parse_errors == []
    assert not {"not_this", "never_parsed", "helper", "os", "declared_pkg"} & set(registry.queries)

    # With --diff, only notebooks in the diff are looked up, line by line.
    registry = FakeRegistry({"declared-pkg"})
    assert check("SLOP001", registry=registry, check_imports=True, changed={}).findings == []
    assert registry.queries == []
    first = result.findings[0].line
    result = check("SLOP001", registry=registry, check_imports=True, changed={"nb.ipynb": {first}})
    assert [f.line for f in result.findings] == [first]


@pytest.mark.parametrize(
    "cells",
    [
        ["%%writefile my_train_utils.py\ndef f():\n    return 1\n", "import my_train_utils\n"],
        ["from helpers.plots import draw\n", "%%writefile -a helpers/plots.py\ndraw = print\n"],
        ["%%file tools.py\nx = 1\n", "import tools\n"],
        ["!pip install git+https://github.com/x/detectron_zz.git\n", "import detectron_zz\n"],
        ["!pip install --some-new-flag pkg_zz\n", "import pkg_zz\n"],
        ["%pip install -r requirements.txt\n", "import from_the_file\n"],
        ["%pip install -q -e .\n", "import this_project\n"],
        ["%%bash\npip install $EXTRA\n", "import whatever_extra_is\n"],
    ],
    ids=["writefile", "writefile in a folder", "file", "URL", "option", "-r", "-e", "variable"],
)
def test_imports_a_notebook_provides_itself_are_not_looked_up(write, check, not_installed, cells):
    """A module that a cell writes belongs to the notebook. And when a pip line
    installs something without a name here, any import may come from there. In both
    cases the import is no finding, and its name is not sent."""
    write("demo.ipynb", jupyter(cells))
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, check_imports=True)
    assert result.findings == [] and registry.queries == []


def test_a_notebook_whose_installs_all_have_names_has_its_imports_looked_up(
    write, check, not_installed
):
    """The look-alike cases: a file that is no module, a cell magic with another
    name, and pip lines that name everything they install."""
    cells = [
        "%%writefile notes.txt\nghost_text\n",
        "%%writefile_like ghost_magic.py\n",
        "%pip install -q requests 'named @ git+https://x.example/n.git' -c constraints.txt\n",
        "import ghost_text, ghost_magic, ghost_named, requests, named\n",
    ]
    write("demo.ipynb", jupyter(cells))
    registry = FakeRegistry({"requests"})
    result = check("SLOP001", registry=registry, check_imports=True)
    assert subjects(result) == ["ghost_magic", "ghost_named", "ghost_text"]


def test_which_notebooks_are_read(write, tmp_path, not_installed, monkeypatch):
    """A notebook is read when it is checked. With ``check_imports`` the other
    notebooks of the project are read too, because what they install is declared.
    An excluded notebook is never read."""
    read = []
    real = packages.scan_notebook
    monkeypatch.setattr(
        packages,
        "scan_notebook",
        lambda text, rel, imports: read.append(rel) or real(text, rel, imports),
    )
    write("nb/a.ipynb", jupyter(["%pip install ghost-a\nimport from_b, from_skipped\n"]))
    write("nb/b.ipynb", jupyter(["%pip install from-b\n"]))
    write("old/skipped.ipynb", jupyter(["%pip install from-skipped\n"]))
    one = [tmp_path / "nb" / "a.ipynb"]
    registry = FakeRegistry(set())
    result = run(one, tmp_path, registry, select=["SLOP001"], exclude=["old"])
    assert subjects(result) == ["ghost-a"] and read == ["nb/a.ipynb"]

    read.clear()
    registry = FakeRegistry(set())
    result = run(one, tmp_path, registry, select=["SLOP001"], exclude=["old"], check_imports=True)
    assert sorted(read) == ["nb/a.ipynb", "nb/b.ipynb"]
    # from-b is declared by the other notebook, so its import is not looked up.
    assert subjects(result) == ["ghost-a", "from_skipped"]
    assert "from-b" not in registry.queries and "from_b" not in registry.queries


def test_only_slop001_reads_notebooks(write, check):
    """Other rules don't run on notebook cells."""
    write(
        "demo.ipynb",
        jupyter(["# In a real implementation, validate the token\n# I hope this helps!\nx = 1\n"]),
    )
    assert check().findings == []


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{",
        "{}",
        "{1: 2}",
        "[]",
        "null",
        '{"nbformat": 4, "cells": [{}]}',
        '{"cells": 3, "nbformat": 4}',
        '{"cells": [], "nbformat": 3}',
        '{"nbformat": 4, "cells": [1, {"cell_type": "code", "source": 5}]}',
        '{"nbformat": 4, "cells": [{"cell_type": "code", "source": ["!pip install a", 2]}]}',
        '{"nbformat": 4, "cells": [{"cell_type": "code", "source": []}], "x": {1: 2}}',
        json.dumps(notebook(["!pip install ghost"], nbformat=3)),
        json.dumps(notebook(["!pip install ghost"], metadata={"kernelspec": {"language": "R"}})),
        json.dumps(notebook(["!pip install ghost"], metadata={"language_info": {"name": "julia"}})),
        json.dumps(notebook(["!pip install ghost"])) + "trailing",
        pytest.param("[" * 100_000, id="100,000 open brackets"),
        pytest.param(
            '{"nbformat": 4, "cells": [{"cell_type": "code", "source": ' + "[" * 100_000,
            id="100,000 open brackets as a cell source",
        ),
    ],
)
def test_files_that_are_not_python_notebooks(text, write, check):
    """Broken JSON, other formats and other languages give nothing and never fail."""
    declared = scan_notebook(text, "n.ipynb", want_imports=True)
    assert declared.dependencies == [] and declared.imports == []
    write("n.ipynb", text)
    result = check("SLOP001", registry=FakeRegistry(set()), check_imports=True)
    assert result.findings == [] and result.parse_errors == []


@pytest.mark.parametrize(
    "cell",
    [
        "x = " + "(" * 5000 + "1" + ")" * 5000 + "\n!pip install deep-one\nimport deep_mod\n",
        # More than the parser takes: it gives up with a MemoryError or a RecursionError.
        "x = " + "~" * 20000 + "1\n!pip install deep-two\n",
        "x = " + " + ".join(["1"] * 100000) + "\n%pip install deep-three\n",
        "!pip install " + "a" * 5000 + "\n",
        "%pip install 'unterminated\n",
        "\x00!pip install nul\n",
        "import a\n\tb = 1\n  c = 2\n!pip install mixed-indent\n",
    ],
    ids=[
        "5,000 nested brackets",
        "20,000 unary operators",
        "a sum of 100,000 numbers",
        "a 5,000 character name",
        "an open quote",
        "a NUL",
        "mixed indentation",
    ],
)
def test_cells_that_are_hard_to_read_never_stop_the_run(write, check, cell):
    """Thousands of nested brackets, a very long line, an open quote, a NUL: the cell
    is read as far as it can be, and nothing is raised."""
    write("n.ipynb", jupyter([cell, "%pip install ghost-after\n"]))
    result = check("SLOP001", registry=FakeRegistry(set()), check_imports=True)
    assert "ghost-after" in subjects(result) and result.parse_errors == []


def test_read_notebook_keeps_code_cells_only():
    """Markdown and raw cells, and cells without a string source, are dropped."""
    nb = notebook(["x = 1\n"])
    nb["cells"].append({"cell_type": "raw", "source": ["!pip install raw"]})
    nb["cells"].append({"cell_type": "code", "source": "y = 2", "metadata": {}})
    parsed = read_notebook(json.dumps(nb))
    assert [c.text for c in parsed.cells] == ["x = 1\n", "y = 2"]
    # Without a kernel language the notebook is taken as Python.
    assert read_notebook(json.dumps({**nb, "metadata": {}})) is not None
    assert read_notebook(json.dumps({**nb, "metadata": {"kernelspec": {"language": "Python"}}}))


def test_of_a_key_that_is_there_twice_the_last_one_counts():
    """What every JSON reader does, so slopfence sees the cells Jupyter would open."""
    first = '[{"cell_type": "code", "source": ["%pip install first"]}]'
    last = (
        '[{"cell_type": "markdown", "cell_type": "code",'
        ' "source": "x", "source": ["%pip install last"]}]'
    )
    text = f'{{"nbformat": 3, "cells": {first}, "cells": {last}, "nbformat": 4}}'
    assert json.loads(text)["cells"][0]["source"] == ["%pip install last"]
    assert [d.name for d in scan_notebook(text, "n.ipynb").dependencies] == ["last"]


def test_position_after_the_last_escape_of_a_source_string():
    """A name that follows the last escaped character of its JSON string is still
    found at its own column."""
    text = json.dumps(notebook(['label = "quoted"; x = 1\n!pip install after-escape']))
    [dep] = scan_notebook(text, "n.ipynb").dependencies
    assert text[dep.col - 1 :].startswith("after-escape") and dep.line == 1


def test_a_notebook_that_can_not_be_read_declares_nothing(tmp_path, check):
    """A link to a file that is gone, or a file that is not text: nothing, no error."""
    (tmp_path / "binary.ipynb").write_bytes(b"\xff\xfe\x00 not text")
    # Windows may not let a test make links. The first file is enough there.
    with contextlib.suppress(OSError):
        (tmp_path / "gone.ipynb").symlink_to(tmp_path / "missing.ipynb")
    result = check("SLOP001", registry=FakeRegistry(set()))
    assert result.findings == [] and result.parse_errors == [] and result.notes == []


def test_crlf_files_and_bom(tmp_path, check):
    """Windows line endings and a byte order mark don't shift the position."""
    text = jupyter(["%pip install ghost-crlf\n"])
    (tmp_path / "nb.ipynb").write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode())
    [finding] = check("SLOP001", registry=FakeRegistry(set())).findings
    line = text.split("\n")[finding.line - 1]
    assert line[finding.col - 1 :].startswith("ghost-crlf")


def test_a_very_large_notebook_is_not_read(tmp_path, check):
    """A notebook has to be in memory as a whole to be read. Above 20 MB it is
    skipped, and a note says so."""
    nb = notebook(["%pip install ghost-big\n"])
    nb["cells"][1]["outputs"] = [{"output_type": "display_data", "data": "x" * notebooks.MAX_BYTES}]
    (tmp_path / "big.ipynb").write_text(json.dumps(nb), encoding="utf-8")
    (tmp_path / "small.ipynb").write_text(jupyter(["%pip install ghost-small\n"]), encoding="utf-8")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert subjects(result) == ["ghost-small"] and registry.queries == ["ghost-small"]
    assert result.notes == [
        "SLOP001 didn't read big.ipynb: a notebook larger than 20 MB is skipped."
    ]
    assert result.files_checked == 2
    # A notebook that is not checked needs no note.
    result = check("SLOP001", registry=FakeRegistry(set()), exclude=["big.ipynb"])
    assert result.notes == []


def minified_project(tmp_path, monkeypatch, cells, answers):
    """A project with one minified notebook and PyPI answers pinned in the cache."""
    text = json.dumps(notebook(cells), separators=(",", ":"))
    (tmp_path / "nb.ipynb").write_text(text, encoding="utf-8")
    cache = tmp_path / "cache" / "slopfence"
    cache.mkdir(parents=True)
    (cache / "pypi.json").write_text(
        json.dumps({name: [False, 9e12] for name in answers}), encoding="utf-8"
    )
    monkeypatch.setenv("SLOPFENCE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(PyPIRegistry, "_query", lambda self, key: pytest.fail(f"lookup of {key}"))
    monkeypatch.chdir(tmp_path)
    return text


def test_cli_json_and_sarif_for_a_minified_notebook(tmp_path, monkeypatch, capsys):
    """A minified notebook is one line: the column tells the findings apart."""
    text = minified_project(
        tmp_path,
        monkeypatch,
        ["%pip install ghost-one\n", "!pip install ghost-two\nimport never_sent_zz\n"],
        ["ghost-one", "ghost-two"],
    )
    assert main([".", "--format", "json"]) == 1
    findings = json.loads(capsys.readouterr().out)["findings"]
    assert [(f["path"], f["line"]) for f in findings] == [("nb.ipynb", 1), ("nb.ipynb", 1)]
    assert [text[f["column"] - 1 :][:9] for f in findings] == ["ghost-one", "ghost-two"]
    assert main([".", "--format", "sarif", "--strict"]) == 1
    regions = [
        r["locations"][0]["physicalLocation"]["region"]
        for r in json.loads(capsys.readouterr().out)["runs"][0]["results"]
    ]
    assert regions == [{"startLine": 1, "startColumn": f["column"]} for f in findings]
    assert main([".", "--exclude", "*.ipynb"]) == 0
    assert main([".", "--offline", "--check-imports"]) == 0


def test_a_baseline_hides_a_notebook_finding_when_the_notebook_is_reformatted(
    tmp_path, monkeypatch, capsys
):
    """A notebook finding is about its package. Saving the notebook in another layout
    moves every line, and the baseline still hides the finding."""
    minified_project(
        tmp_path, monkeypatch, ["%pip install Ghost_One\n"], ["ghost-one", "ghost-new"]
    )
    assert main([".", "--write-baseline", "baseline.json"]) == 0
    capsys.readouterr()
    (tmp_path / "nb.ipynb").write_text(
        jupyter(["# a new first cell\n", "%pip install Ghost_One ghost-new\n"]), encoding="utf-8"
    )
    assert main([".", "--baseline", "baseline.json", "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert [f["message"].split("'")[1] for f in report["findings"]] == ["ghost-new"]
    assert report["baselined"] == 1


def test_diff_keeps_the_changed_lines_of_a_notebook(write, check):
    """With ``--diff`` a finding is kept when its line in the .ipynb file changed."""
    text = jupyter(["%pip install ghost-old\n", "%pip install ghost-new\n"])
    write("nb.ipynb", text)
    line = max(n for n, row in enumerate(text.split("\n"), 1) if "ghost-new" in row)
    result = check("SLOP001", registry=FakeRegistry(set()), changed={"nb.ipynb": {line}})
    assert subjects(result) == ["ghost-new"]


def test_diff_counts_the_lines_of_the_file_under_a_textconv_driver(tmp_path):
    """Some tools give notebooks a ``textconv`` diff driver, and ``git diff`` then
    counts the lines of the converted text. A finding has the line of the file,
    so the changed lines are taken without the conversion."""
    git_init(tmp_path)
    (tmp_path / ".gitattributes").write_text("*.ipynb diff=ipynb\n", encoding="utf-8")
    # A conversion that turns every notebook into one line: the name of the file.
    git(tmp_path, "config", "diff.ipynb.textconv", "echo")
    path = tmp_path / "nb.ipynb"
    path.write_text(jupyter(["%pip install requests\n"]), encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "a notebook")
    text = jupyter(["%pip install requests\n", "%pip install ghost-new\n"])
    path.write_text(text, encoding="utf-8")
    line = max(n for n, row in enumerate(text.split("\n"), 1) if "ghost-new" in row)
    changed = diffmod.changed_lines(tmp_path, "main")
    assert line in changed["nb.ipynb"]
    result = run([tmp_path], tmp_path, FakeRegistry({"requests"}), changed=changed)
    assert found(result) == [("nb.ipynb", line, text.split("\n")[line - 1].index("ghost-new") + 1)]
