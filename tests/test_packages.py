import pytest

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
