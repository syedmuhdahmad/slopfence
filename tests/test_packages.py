import textwrap

import pytest

from slopfence.detectors import packages
from slopfence.detectors.packages import parse_dependency_file
from tests.conftest import DownRegistry, FakeRegistry, rule_lines


def test_requirements_nonexistent_dependency(write, check):
    write(
        "requirements.txt",
        """
        # web deps
        requests>=2.31
        flask-jwt-simple-auth==1.2.0
        -r other.txt
        git+https://github.com/org/repo.git
        local-thing @ https://example.com/pkg.whl
        made-up-but-ignored  # slopfence: ignore
        """,
    )
    result = check("SLOP001", registry=FakeRegistry({"requests"}))
    assert rule_lines(result, "SLOP001") == [3]
    assert "flask-jwt-simple-auth" in result.findings[0].message


def test_pyproject_dependencies(write, check, tmp_path):
    """PEP 621 dependencies, optional dependencies and dependency groups are read. The
    project's own name is not one of them."""
    write(
        "pyproject.toml",
        """
        [project]
        name = "demo"
        dependencies = ["requests>=2", "totally-fake-pkg[extra]>=1"]

        [project.optional-dependencies]
        dev = ["pytest", "demo[extra]"]

        [dependency-groups]
        lint = ["ruff", {include-group = "dev"}]
        """,
    )
    declared = parse_dependency_file(tmp_path / "pyproject.toml", tmp_path)
    deps = [d.name for d in declared.dependencies]
    assert deps == ["requests", "totally-fake-pkg", "pytest", "ruff"]
    result = check("SLOP001", registry=FakeRegistry({"requests", "pytest", "ruff"}))
    assert rule_lines(result, "SLOP001") == [3]


def test_poetry_dependencies(tmp_path, write):
    """Poetry tables are read. ``python`` is not a package, and a path dependency is
    declared but not looked up."""
    write(
        "pyproject.toml",
        """
        [tool.poetry.dependencies]
        python = "^3.11"
        httpx = "^0.27"
        mylib = { path = "../mylib" }

        [tool.poetry.group.dev.dependencies]
        pytest = "^8"
        """,
    )
    deps = parse_dependency_file(tmp_path / "pyproject.toml", tmp_path).dependencies
    # Path dependencies stay declared (so `import mylib` isn't flagged) but aren't looked up.
    assert [(d.name, d.line, d.validate) for d in deps] == [
        ("httpx", 3, True),
        ("mylib", 4, False),
        ("pytest", 7, True),
    ]


def test_hallucinated_import_flagged(write, check):
    """An import of a package missing from PyPI is reported; stdlib and local ones aren't."""
    write(
        "src/app.py",
        """
        import os
        import json
        from collections import OrderedDict
        import yaml
        import utils
        from . import sibling
        import fastjsonvalidatorx
        from fastjsonvalidatorx.core import validate
        """,
    )
    write("src/utils.py", "")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, check_imports=True)
    # Every occurrence is reported, so --diff and ignores work per line.
    assert rule_lines(result, "SLOP001") == [7, 8]
    # stdlib, local modules and known aliases are never looked up.
    assert set(registry.queries) == {"fastjsonvalidatorx"}


def test_installed_and_declared_imports_not_flagged(write, check):
    """Installed and declared packages are trusted without asking PyPI about the import."""
    write("requirements.txt", "my-declared-lib\n")
    write("src/app.py", "import pytest\nimport my_declared_lib\n")
    registry = FakeRegistry({"my-declared-lib"})
    assert check("SLOP001", registry=registry, check_imports=True).findings == []
    assert "pytest" not in registry.queries


def test_unreachable_registry_never_flags(write, check):
    """If PyPI can't be reached, nothing is reported as missing."""
    write("requirements.txt", "anything-at-all\n")
    write("src/app.py", "import nonexistent_xyz\n")
    assert check("SLOP001", registry=DownRegistry(), check_imports=True).findings == []


def test_imports_are_not_looked_up_by_default(write, check):
    """Only dependency files are checked by default: sending import names to PyPI
    can leak private package names, so import lookups are opt-in (#34)."""
    write("requirements.txt", "ghost-dep\n")
    write("src/app.py", "import corp_secret_auth\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert [(f.path, f.line) for f in result.findings] == [("requirements.txt", 1)]
    assert registry.queries == ["ghost-dep"]


def test_offline_skips_lookups(write, check):
    write("requirements.txt", "fake-pkg\n")
    write("src/app.py", "import fake_pkg_two\n")
    assert check("SLOP001").findings == []


def test_single_file_still_knows_project_modules(write, tmp_path):
    """Checking one file still resolves the project's own modules and dependencies."""
    from slopfence.engine import run

    write("requirements.txt", "my-internal-dep\n")
    write("src/mypkg/__init__.py", "")
    write("src/mypkg/core.py", "")
    app = write("src/app.py", "import mypkg.core\nimport my_internal_dep\nimport invented_pkg_zz\n")
    registry = FakeRegistry({"my-internal-dep"})
    result = run([app], tmp_path, registry, select=["SLOP001"], check_imports=True)
    assert rule_lines(result, "SLOP001") == [3]
    assert set(registry.queries) == {"invented_pkg_zz", "invented-pkg-zz"}


def test_requirements_ignore_scopes(write, check):
    write(
        "requirements.txt",
        """
        fake-one  # slopfence: ignore[SLOP001]
        fake-two  # slopfence: ignore[SLOP051]
        fake-three  # slopfence: ignore
        """,
    )
    result = check("SLOP001", registry=FakeRegistry(set()))
    # A rule-specific ignore for another rule must not hide SLOP001.
    assert rule_lines(result, "SLOP001") == [2]


def test_requirements_ignore_file(write, check):
    write("requirements.txt", "# slopfence: ignore-file\nfake-one\nfake-two\n")
    assert check("SLOP001", registry=FakeRegistry(set())).findings == []


def test_ignored_and_external_requirements_stay_declared(write, check):
    """Ignored and non-PyPI requirements still count as declared for imports."""
    write(
        "requirements.txt",
        """
        internal-lib  # slopfence: ignore
        internal-client @ https://example.com/internal.whl
        git+https://example.com/repo.git#egg=git-thing
        """,
    )
    write("src/app.py", "import internal_lib\nimport internal_client\nimport git_thing\n")
    registry = FakeRegistry(set())
    assert check("SLOP001", registry=registry, check_imports=True).findings == []
    assert registry.queries == []  # none of them are looked up on PyPI


def test_private_index_skips_validation(write, check):
    write("requirements.txt", "--extra-index-url https://pkgs.example.com/simple\ncorp-lib\n")
    assert check("SLOP001", registry=FakeRegistry(set())).findings == []


@pytest.mark.parametrize(
    ("config", "private"),
    [
        ('[[tool.poetry.source]]\nname = "corp"\nurl = "https://pkgs.corp.example/simple"\n', True),
        (
            '[[tool.poetry.source]]\nname = "corp"\nurl = "https://pkgs.corp.example/simple"\n'
            'priority = "supplemental"\n',
            True,
        ),
        # Explicit sources only serve dependencies that name them (checked per dependency).
        (
            '[[tool.poetry.source]]\nname = "corp"\nurl = "https://pkgs.corp.example/simple"\n'
            'priority = "explicit"\n',
            False,
        ),
        ('[[tool.poetry.source]]\nname = "mirror"\nurl = "https://pypi.org/simple"\n', False),
        ('[[tool.uv.index]]\nname = "corp"\nurl = "https://pkgs.corp.example/simple"\n', True),
        (
            '[[tool.uv.index]]\nname = "corp"\nurl = "https://pkgs.corp.example/simple"\n'
            "explicit = true\n",
            False,
        ),
        ('[tool.uv]\nextra-index-url = ["https://pkgs.corp.example/simple"]\n', True),
        ('[tool.uv]\nindex-url = "https://pypi.org/simple"\n', False),
        # Matched by host, so a private URL that merely contains "pypi.org" stays private.
        ('[[tool.uv.index]]\nname = "corp"\nurl = "https://pypi.org.corp.example/simple"\n', True),
        ('[[tool.poetry.source]]\nname = "corp"\nurl = "https://corp.example/pypi.org/"\n', True),
    ],
)
def test_project_wide_private_index(tmp_path, write, config, private):
    """A project-wide private Poetry or uv index means names missing from PyPI may be
    private, so no dependency in that file is looked up on PyPI."""
    write(
        "pyproject.toml",
        '[project]\nname = "demo"\ndependencies = ["corp-auth"]\n\n'
        '[tool.poetry.dependencies]\ncorp-billing = "^1"\n\n' + config,
    )
    deps = parse_dependency_file(tmp_path / "pyproject.toml", tmp_path).dependencies
    assert {d.name: d.validate for d in deps} == {
        "corp-auth": not private,
        "corp-billing": not private,
    }


@pytest.mark.parametrize(
    ("option", "private"),
    [
        ("--index-url https://pypi.org/simple", False),
        ("-i https://pypi.org/simple", False),
        ("--extra-index-url=https://files.pythonhosted.org/simple", False),
        ("--extra-index-url https://pkgs.corp.example/simple", True),
        ("--index-url https://corp.example/pypi.org/simple", True),
        ("-i https://pypi.org.corp.example/simple  # mirror", True),
    ],
)
def test_requirements_index_matched_by_host(write, check, option, private):
    """Only PyPI's own hosts count as public; a URL that merely contains "pypi.org"
    is a private index, so its packages are neither reported nor looked up."""
    write("requirements.txt", f"{option}\ncorp-lib\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert registry.queries == ([] if private else ["corp-lib"])
    assert rule_lines(result, "SLOP001") == ([] if private else [2])


def test_pyproject_line_is_the_dependency_entry(write, check):
    write(
        "pyproject.toml",
        """
        [project]
        name = "demo"
        description = "ghost-lib"
        dependencies = [
            # "ghost-lib",
            "ghost-lib",
            "other-ghost",  # slopfence: ignore[SLOP001]
        ]
        """,
    )
    result = check("SLOP001", registry=FakeRegistry(set()))
    assert rule_lines(result, "SLOP001") == [6]


def test_ignore_comment_applies_to_entry_written_with_escapes(write, check):
    """An ignore comment keeps an entry with escaped quotes on the machine, in every
    array of requirement strings: the entry is found on its own line."""
    write(
        "pyproject.toml",
        r"""
        [project]
        name = "demo"
        dependencies = [
            "corp-lib; python_version<\"3.12\"",  # slopfence: ignore[SLOP001]
            "corp-lib2; python_version<'3.12'",  # slopfence: ignore[SLOP001]
        ]

        [project.optional-dependencies]
        dev = [
            "corp-dev; os_name == \u0022nt\u0022",  # slopfence: ignore[SLOP001]
        ]

        [dependency-groups]
        lint = [
            "corp-lint; sys_platform == \"win32\"",  # slopfence: ignore[SLOP001]
        ]
        """,
    )
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert registry.queries == []
    assert rule_lines(result, "SLOP001") == []


@pytest.mark.parametrize(
    "array",
    [
        r"""
        dependencies = [
            "corp-a; platform_version == \"]\"",  # slopfence: ignore[SLOP001]
            "corp-b",  # slopfence: ignore[SLOP001]
        ]
        """,
        """
        dependencies = [  # was "corp-b"
            "corp-b",  # slopfence: ignore[SLOP001]
        ]
        """,
    ],
    ids=["escaped-bracket", "name-in-comment"],
)
def test_ignore_comment_applies_after_a_bracket_or_name_outside_an_entry(write, check, array):
    """A bracket in an earlier entry's string does not end the array, and a name in
    a comment is no entry: the entry is found on its own line, with its comment."""
    write("pyproject.toml", '[project]\nname = "demo"\n' + textwrap.dedent(array))
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert registry.queries == []
    assert rule_lines(result, "SLOP001") == []


@pytest.mark.parametrize(
    "array",
    [
        r"""
        dependencies = [  # slopfence: ignore[SLOP001]
            "corp-lib; python_version<\"3.12\"",
        ]
        """,
        r"""
        [project.optional-dependencies]
        dev = [  # slopfence: ignore[SLOP001]
            "corp-dev; os_name == \"nt\"",
        ]
        """,
        r"""
        dependencies = [  # slopfence: ignore[SLOP001]
            "corp-a; platform_release == \"5.0]\"",
            "corp-b",
        ]
        """,
        """
        dependencies = [
            \"\"\"corp-lib; extra == \"\"\"\"\",  # slopfence: ignore[SLOP001]
        ]
        """,
    ],
    ids=["escaped-entry", "optional-escaped-entry", "after-escaped-bracket", "five-quotes"],
)
def test_ignore_comment_where_v03_reported_an_entry_still_applies(write, check, array):
    """v0.3 reported these entries on the line that opens their array (or found the
    entry where it is, for the five quotes), so an ignore comment there kept their
    names on the machine. It still does."""
    write("pyproject.toml", '[project]\nname = "demo"\n' + textwrap.dedent(array))
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert registry.queries == []
    assert rule_lines(result, "SLOP001") == []


def test_ignore_comment_on_the_array_line_leaves_a_plain_entry_reported(write, check):
    """A comment on the line that opens an array never covered an entry that v0.3
    found on its own line, and it still does not."""
    write(
        "pyproject.toml",
        """
        [project]
        name = "demo"
        dependencies = [  # slopfence: ignore[SLOP001]
            "ghost-lib",
        ]
        """,
    )
    result = check("SLOP001", registry=FakeRegistry(set()))
    assert rule_lines(result, "SLOP001") == [4]


@pytest.mark.parametrize(
    "entry",
    [
        r'"ghost-lib; python_version<\"3.12\""',
        r'"ghost-lib; python_version<\u00223.12\u0022"',
        r'"ghost-lib; platform_version == \"C:\\x\""',
        """'''ghost-lib; python_version<"3.12"'''""",
        '"""ghost-lib; python_version<"3.12""""',
    ],
    ids=[
        "escaped-quote",
        "unicode-escape",
        "escaped-backslash",
        "multi-line-literal",
        "quote-before-closing-quotes",
    ],
)
def test_entry_written_with_escapes_is_reported_on_its_own_line(write, check, entry):
    """Without an ignore comment, an entry written with TOML escapes is still
    reported, on its own line rather than the line that opens the array."""
    write(
        "pyproject.toml",
        f"""
        [project]
        name = "demo"
        dependencies = [
            "requests",
            {entry},
        ]
        """,
    )
    registry = FakeRegistry({"requests"})
    result = check("SLOP001", registry=registry)
    assert registry.queries == ["requests", "ghost-lib"]
    assert rule_lines(result, "SLOP001") == [5]


def test_escaped_quotes_in_a_multi_line_string_end_it_nowhere(write, check):
    """A ``\\\"\"\"`` inside a multi-line string does not end it, so the entries after
    it are found on their own lines, and their ignore comments apply."""
    write(
        "pyproject.toml",
        r'''
        [project]
        name = "demo"
        description = """He said \""" hi"""
        dependencies = [
            "ghost-lib",
            "corp-lib",  # slopfence: ignore[SLOP001]
        ]
        ''',
    )
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert registry.queries == ["ghost-lib"]
    assert rule_lines(result, "SLOP001") == [5]


def test_pyproject_external_sources(tmp_path, write):
    """A dependency from a URL, git or a named source is declared but not looked up."""
    write(
        "pyproject.toml",
        """
        [project]
        name = "demo"
        dependencies = ["corp-pkg @ https://example.com/corp.whl", "uv-pkg"]

        [tool.uv.sources]
        uv-pkg = { git = "https://example.com/uv-pkg.git" }

        [tool.poetry.dependencies]
        url-dep = { url = "https://example.com/u.whl" }
        private-dep = { version = "^1", source = "corp" }
        """,
    )
    deps = parse_dependency_file(tmp_path / "pyproject.toml", tmp_path).dependencies
    assert {d.name: d.validate for d in deps} == {
        "corp-pkg": False,
        "uv-pkg": False,
        "url-dep": False,
        "private-dep": False,
    }


def test_every_import_occurrence_reported(write, check):
    """Each import of a missing package is reported on its own line."""
    write(
        "src/app.py",
        """
        import ghostpkg_q  # slopfence: ignore
        import ghostpkg_q
        """,
    )
    result = check("SLOP001", registry=FakeRegistry(set()), check_imports=True)
    # The ignored first occurrence must not hide the second one.
    assert rule_lines(result, "SLOP001") == [2]


def test_directives_inside_toml_strings_are_ignored(write, check):
    """``# slopfence: ignore`` only counts in a real TOML comment: not in a
    description, a multi-line string or the marker of a requirement."""
    write(
        "pyproject.toml",
        """
        [project]
        name = "demo"
        description = "# slopfence: ignore-file"
        readme = \"\"\"
        # slopfence: ignore-file
        \"\"\"
        dependencies = [
            "ghost-one",
            "ghost-two",  # slopfence: ignore
            "ghost-three ; extra == '# slopfence: ignore'",
        ]
        """,
    )
    result = check("SLOP001", registry=FakeRegistry(set()))
    assert rule_lines(result, "SLOP001") == [8, 10]


def test_include_group_dependencies_are_checked_once(write, check):
    # Each group is parsed on its own, so included entries are already checked.
    write(
        "pyproject.toml",
        """
        [project]
        name = "demo"

        [dependency-groups]
        dev = ["fake-pkg"]
        lint = [{include-group = "dev"}, "ruff"]
        """,
    )
    result = check("SLOP001", registry=FakeRegistry({"ruff"}))
    assert rule_lines(result, "SLOP001") == [5]


def test_toml_comments():
    from slopfence.detectors.packages import toml_comments

    text = 'a = "x # no"  # yes\nb = \'# no\'\nc = """\n# no\n"""  # end\n# top'
    assert toml_comments(text) == ["# yes", "", "", "", "# end", "# top"]


def test_toml_strings_are_read_as_toml_reads_them():
    """Each string is kept on the line it starts on and decoded by TOML's reader; a
    string in a comment is none, and so is one cut short."""
    from slopfence.detectors.packages import _toml_lines, _toml_string

    text = 'a = ["x\\"]", \'z\']  # ["no"]\nb = """one\ntwo"""'
    lines = _toml_lines(text)
    values = [[_toml_string(source) for _, source in line.strings] for line in lines]
    assert values == [['x"]', "z"], ["one\ntwo"], []]
    assert _toml_string('"cut') is None


# --- The line of each entry: the same answers for a file of any size. ---


def _entries(path, root):
    """(name, line, rules switched off) of each dependency of a file, in order."""
    found = parse_dependency_file(path, root)
    return [(dep.name, dep.line, sorted(dep.ignored)) for dep in found.dependencies]


def test_a_repeated_entry_takes_the_next_place_in_its_own_array(write, tmp_path):
    """Each copy of a requirement string is found at the next place that holds it, in
    its own array: two on one line, however it is quoted, and never the commented-out
    copy, the one in a description or the one in an array of the same name."""
    path = write(
        "pyproject.toml",
        """
        [tool.other]
        dependencies = ["ghost-lib"]  # slopfence: ignore

        [project]
        name = "demo"
        description = "ghost-lib"  # slopfence: ignore
        dependencies = [
            # "ghost-lib",  # slopfence: ignore
            "ghost-lib", "ghost-lib",  # slopfence: ignore[SLOP002]
            "ghost-lib",  # slopfence: ignore[SLOP001]
            'ghost-lib',
        ]

        [project.optional-dependencies]
        dev = ["ghost-lib"]  # slopfence: ignore[SLOP030]
        test = ["ghost-lib"]

        [dependency-groups]
        dev = [
            "ghost-lib"]
        """,
    )
    assert _entries(path, tmp_path) == [
        ("ghost-lib", 9, ["SLOP002"]),
        ("ghost-lib", 9, ["SLOP002"]),
        ("ghost-lib", 10, ["SLOP001"]),
        ("ghost-lib", 11, []),
        ("ghost-lib", 15, ["SLOP030"]),
        ("ghost-lib", 16, []),
        ("ghost-lib", 20, []),
    ]


def test_each_poetry_entry_is_found_on_the_line_of_its_own_key(write, tmp_path):
    """A key is found whole, in any way of writing it: not where a longer or a shorter
    name starts the line, not in a comment, and in its own table only. The comment on
    the table header still covers the entries that v0.2 reported there."""
    path = write(
        "pyproject.toml",
        """
        [tool.poetry.dependencies]  # slopfence: ignore[SLOP002]
        python = "^3.10"
        # ghost = "1"  # slopfence: ignore
        ghost-lib = "1"
        ghost = "1"
          "gho" = "1"  # slopfence: ignore[SLOP001]
        'ghost.ext'="1"
        ghost-dotted.version = "1"
        " ghost" = "1"

        [tool.poetry.dependencies.ghost-own]
        version = "1"

        [tool.poetry.group.dev.dependencies]
        ghost = "2"  # slopfence: ignore[SLOP030]
        """,
    )
    assert _entries(path, tmp_path) == [
        ("ghost-lib", 4, []),
        ("ghost", 5, []),
        ("gho", 6, ["SLOP001"]),
        ("ghost.ext", 7, []),
        ("ghost-dotted", 8, ["SLOP002"]),
        (" ghost", 9, []),
        ("ghost-own", 11, ["SLOP002"]),
        ("ghost", 15, ["SLOP030"]),
    ]


def test_each_pipfile_entry_is_found_on_the_line_of_its_own_key(write, tmp_path):
    """The same in a Pipfile, whose two tables are read in a fixed order: a name that
    both hold is found once in each, and a comment that looks like the entry is none."""
    path = write(
        "Pipfile",
        """
        [dev-packages]
        ghost = "*"  # slopfence: ignore[SLOP002]
        #hash = "*"

        [packages]
        #hash = "*"  # slopfence: ignore
        "#hash" = "*"
        ghost-lib = "*"
        ghost = "*"  # slopfence: ignore[SLOP001]
        [packages.ghost-own]
        version = "*"
        """,
    )
    assert _entries(path, tmp_path) == [
        ("#hash", 7, []),
        ("ghost-lib", 8, []),
        ("ghost", 9, ["SLOP001"]),
        ("ghost-own", 10, []),
        ("ghost", 2, ["SLOP002"]),
    ]


@pytest.mark.parametrize(
    "array",
    [
        """
        dependencies = [  # slopfence: ignore[SLOP001]
            'ghost-lib; python_version < "3.12"',
        ]
        """,
        """
        dependencies = [  # "ghost-lib' slopfence: ignore[SLOP001]
            "ghost-lib",
        ]
        """,
        """
        dependencies = [  # 'ghost-lib" slopfence: ignore[SLOP001]
            "ghost-lib"]
        """,
    ],
    ids=["quotes-in-the-entry", "quotes-that-differ", "quotes-that-differ-the-other-way"],
)
def test_ignore_comment_on_the_array_line_leaves_an_entry_v03_found_reported(write, check, array):
    """v0.3 found these entries on their own line: one in single quotes with double
    quotes inside, and one whose name the array line holds between quotes that do not
    pair up. A comment on the array line never covered them, and still does not."""
    write("pyproject.toml", '[project]\nname = "demo"\n' + textwrap.dedent(array))
    result = check("SLOP001", registry=FakeRegistry(set()))
    assert rule_lines(result, "SLOP001") == [5]


def test_ignore_comment_on_a_commented_out_line_that_opens_an_array_still_applies(write, check):
    """An array starts at the first line that sets its key, also one that is commented
    out. v0.3 reported an entry written with escapes there, so the ignore comment of
    that line kept its name on the machine. It still does."""
    write(
        "pyproject.toml",
        r"""
        [project]
        name = "demo"

        [dependency-groups]
        #x = []  # slopfence: ignore[SLOP001]
        "#x" = [
            "corp-lib; python_version<\"3.12\"",
        ]
        """,
    )
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry)
    assert registry.queries == []
    assert rule_lines(result, "SLOP001") == []


def test_a_match_inside_another_is_none_for_v03():
    """v0.3 went on behind each match on a line. Of ``"a"a"a"`` in a comment it took
    the first and the last ``"a"``, not the one between them that shares their quotes."""
    finder = packages._LineFinder('[project]\ndependencies = [\n    "a",  # "a"a"a"\n]\n')
    found = [finder.spec_in_v03("project", "dependencies", "a") for _ in range(4)]
    # Three matches on line 3, and then none: the line that opens the array.
    assert found == [3, 3, 3, 2]


def test_white_space_in_front_of_a_key_can_be_the_indentation_of_its_line():
    """A key that starts with a space is also found bare on an indented line, as the
    pattern for it always was: the indentation stands for the space."""
    finder = packages._LineFinder('[packages]\n  x = "*"\n" x" = "*"\n')
    assert [finder.key("packages", " x") for _ in range(3)] == [2, 3, 1]
    assert [finder.key_in_v02("packages", " x") for _ in range(3)] == [2, 3, 1]


class _Counted(list):
    """A list that notes each time one of its items is read."""

    def __init__(self, items, reads):
        """``reads`` gets one more item for each read."""
        super().__init__(items)
        self.reads = reads

    def __getitem__(self, index):
        """Read an item, and note that it was done."""
        self.reads.append(index)
        return super().__getitem__(index)


class _CountedCells(set):
    """A set of taken matches that notes each time it is asked about one."""

    def __init__(self, reads):
        """``reads`` gets one more item for each question."""
        super().__init__()
        self.reads = reads

    def __contains__(self, cell):
        """Whether a match is taken, and note that it was asked."""
        self.reads.append(cell)
        return super().__contains__(cell)


def _reads_of_a_file_with(count, write, tmp_path, monkeypatch):
    """How often a line, the code of a line or a taken match is read to find the lines
    of a pyproject.toml and a Pipfile with ``count`` entries of each kind."""
    reads = []

    class Counting(packages._LineFinder):
        """A finder that counts what it reads."""

        def __init__(self, text):
            """Read the text, then count every later read."""
            super().__init__(text)
            self.lines = _Counted(self.lines, reads)
            self._code = _Counted(self._code, reads)
            self._code_in_v03 = _Counted(self._code_in_v03, reads)
            self.used = _CountedCells(reads)
            self._used_by_v02 = _CountedCells(reads)
            self._used_by_v03 = _CountedCells(reads)

    monkeypatch.setattr(packages, "_LineFinder", Counting)
    names = [f"pkg-{n:04d}" for n in range(count)]
    lines = ["[project]", 'name = "demo"', "dependencies = ["]
    lines += [f'    "{name}>=1.0",' for name in names]
    lines += ["]", "[project.optional-dependencies]", "same = ["]
    lines += ['    "same-lib",'] * count
    lines += ["]", "one-line = [" + ", ".join(f'"{name}"' for name in names) + "]"]
    lines += [f'group-{name} = ["{name}"]' for name in names]
    lines += ["[tool.poetry.dependencies]"]
    lines += [f'{name} = "^1.0"' for name in names]
    for name in names:
        lines += [f"[tool.poetry.group.{name}.dependencies]", 'same-lib = "^1.0"']
    pyproject = write("pyproject.toml", "\n".join(lines) + "\n")
    pipfile = write("Pipfile", "[packages]\n" + "".join(f'{name} = "*"\n' for name in names))
    assert len(parse_dependency_file(pyproject, tmp_path).dependencies) == 6 * count
    assert len(parse_dependency_file(pipfile, tmp_path).dependencies) == count
    return len(reads)


def test_a_long_file_is_gone_through_once_not_once_per_entry(write, tmp_path, monkeypatch):
    """A file with twice the entries takes about twice the reads to find their lines,
    whether the entries are requirement strings (each on its own line, all on one
    line, or the same one again and again), arrays of their own, keys of one table, or
    the same key in many tables. With every line read again for every entry it took
    four times as many, and a ``pyproject.toml`` of 20,000 dependencies took minutes."""
    small = _reads_of_a_file_with(100, write, tmp_path, monkeypatch)
    large = _reads_of_a_file_with(200, write, tmp_path, monkeypatch)
    assert 0 < small < large < 2.5 * small, (small, large)
