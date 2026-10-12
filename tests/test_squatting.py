"""SLOP002 (#39): dependencies that exist on PyPI but are too new to trust without a look."""

import json
import time
from types import SimpleNamespace

import pytest

from slopfence import cli
from slopfence.baseline import Baseline, key_of
from slopfence.cli import main
from slopfence.config import ConfigError, load_config, selected_rules, slop002_has_no_lookups
from slopfence.detectors.packages import PACKAGE_RULES, may_look_up
from slopfence.detectors.squatting import NEW, QUARANTINED, SINGLE_RELEASE, assess
from slopfence.engine import SLOP002_NEEDS_SLOP001, run
from slopfence.listing import PackageInfo
from slopfence.models import RULES, Severity
from slopfence.registry import Lookup, OfflineRegistry, PyPIRegistry, normalize
from slopfence.source import ALL_RULES, line_ignores
from tests.conftest import DAY, NOW, FakeFactsRegistry, FakeRegistry, rule_lines, uploaded
from tests.test_notebooks import jupyter, notebook

CHECK = "check its PyPI page and make sure it is the package you mean (slopsquatting risk)"


def findings(result):
    """(rule, path, line) of every finding."""
    return [(f.rule, f.path, f.line) for f in result.findings]


def new(*names, days=3, releases=1):
    """Facts that make each of ``names`` a package SLOP002 reports."""
    return {name: uploaded(days, releases) for name in names}


def pypi(*names, existing=()):
    """A registry in which ``names`` are new packages and ``existing`` are old ones."""
    return FakeFactsRegistry(set(existing), new(*names))


# --- Should be flagged ----------------------------------------------------------------


def test_new_dependency_is_flagged(write, check):
    """A package first uploaded 5 days ago: SLOP001 is silent, SLOP002 is not."""
    write("requirements.txt", "requests>=2\nFlask_JWT.simple-auth==1.2.0\n")
    registry = FakeFactsRegistry({"requests"}, {"flask-jwt-simple-auth": uploaded(5, releases=1)})
    result = check(registry=registry, now=NOW)
    [finding] = result.findings
    assert (finding.rule, finding.path, finding.line, finding.col) == (
        "SLOP002",
        "requirements.txt",
        2,
        1,
    )
    # The name as it is written in the file, the date and not the age.
    assert finding.message == (
        "Dependency 'Flask_JWT.simple-auth' is new on PyPI (oldest upload 2026-10-06, 1 release): "
        "check its PyPI page and make sure it is the package you mean (slopsquatting risk)"
    )
    assert finding.severity is Severity.HIGH and finding.subject == "flask-jwt-simple-auth"
    assert registry.queries == ["requests", "Flask_JWT.simple-auth"]  # nothing extra is sent
    assert registry.info_queries == ["requests", "Flask_JWT.simple-auth"]


def test_many_releases_do_not_make_a_new_package_old(write, check):
    """A real case from PyPI's update feed: 12 releases in the first week."""
    write("requirements.txt", "busy-new-thing\n")
    result = check(registry=FakeFactsRegistry(set(), {"busy-new-thing": uploaded(29, 12)}), now=NOW)
    assert rule_lines(result, "SLOP002") == [1]
    assert "(oldest upload 2026-09-12, 12 releases)" in result.findings[0].message


def test_single_release_package_is_flagged_for_a_year(write, check):
    """A name someone registered once and left: 132 days old, one release."""
    write("requirements.txt", "acme-token-utils\n")
    registry = FakeFactsRegistry(set(), {"acme-token-utils": uploaded(132, releases=1)})
    result = check(registry=registry, now=NOW)
    assert rule_lines(result, "SLOP002") == [1]
    assert result.findings[0].message == (
        "Dependency 'acme-token-utils' has only one release on PyPI (uploaded 2026-06-01): "
        "check its PyPI page and make sure it is the package you mean (slopsquatting risk)"
    )
    almost_a_year = FakeFactsRegistry(set(), {"acme-token-utils": uploaded(364, releases=1)})
    assert rule_lines(check(registry=almost_a_year, now=NOW), "SLOP002") == [1]


def test_quarantined_package_is_flagged(write, check):
    """PyPI's own marker for suspected malware, whatever the package's age."""
    write("requirements.txt", "acme-color-logs\nOld_And.Held\n")
    facts = {
        "acme-color-logs": PackageInfo(None, 0, "quarantined"),
        "old-and-held": uploaded(3000, releases=40, status="quarantined"),
    }
    result = check(registry=FakeFactsRegistry(set(), facts), now=NOW)
    assert [(f.message, f.subject) for f in result.findings] == [
        (
            "Dependency 'acme-color-logs' is quarantined on PyPI as suspected malware: "
            "remove it until PyPI clears it",
            "acme-color-logs quarantined",
        ),
        (
            "Dependency 'Old_And.Held' is quarantined on PyPI as suspected malware: "
            "remove it until PyPI clears it",
            "old-and-held quarantined",
        ),
    ]


SOURCES = {
    "requirements.txt": ("old-pkg\nnew-pkg>=1\n", 2, 1),
    "requirements/dev.in": ("old-pkg==1.0\nnew-pkg[extra]\n", 2, 1),
    "pyproject.toml": (
        '[project]\nname = "demo"\ndependencies = ["old-pkg", "new-pkg>=0.1"]\n',
        3,
        1,
    ),
    "poetry/pyproject.toml": (
        '[tool.poetry.dependencies]\npython = "^3.11"\nnew-pkg = "^0.1"\n',
        3,
        1,
    ),
    "setup.cfg": ("[options]\ninstall_requires =\n    old-pkg\n    new-pkg>=1\n", 4, 1),
    "setup.py": ("from setuptools import setup\n\nsetup(install_requires=['new-pkg'])\n", 3, 25),
    "Pipfile": ('[packages]\nold-pkg = "*"\n\n[dev-packages]\nnew-pkg = "*"\n', 5, 1),
}


@pytest.mark.parametrize("rel", SOURCES)
def test_every_kind_of_dependency_file_is_checked(write, check, rel):
    """Wherever SLOP001 reads a dependency, SLOP002 reads its answer, on the same line."""
    content, line, col = SOURCES[rel]
    write(rel, content)
    result = check(registry=pypi("new-pkg", existing={"old-pkg"}), now=NOW)
    assert [(f.rule, f.path, f.line, f.col) for f in result.findings] == [
        ("SLOP002", rel, line, col)
    ]
    assert result.findings[0].message.startswith("Dependency 'new-pkg' is new on PyPI (oldest")


def test_pip_install_line_in_a_notebook_is_checked(write, check):
    """A ``pip install`` line declares a dependency: line and column are the package's."""
    text = jupyter(["%pip install old-pkg new-pkg\n"])
    write("demo.ipynb", text)
    [finding] = check(registry=pypi("new-pkg", existing={"old-pkg"}), now=NOW).findings
    assert (finding.rule, finding.subject) == ("SLOP002", "new-pkg")
    assert finding.message.startswith("Dependency 'new-pkg' is new on PyPI")
    assert text.splitlines()[finding.line - 1][finding.col - 1 :].startswith("new-pkg")


def test_undeclared_import_of_a_new_package_is_flagged_with_check_imports(write, check):
    """The slopsquatting path: the assistant writes the import, the developer installs it."""
    write("requirements.txt", "requests\n")
    write("src/app.py", "import requests\nimport fastjsonvalidatorx\nimport ghost_pkg_zz\n")
    registry = pypi("fastjsonvalidatorx", existing={"requests"})
    result = check(registry=registry, check_imports=True, now=NOW)
    assert findings(result) == [("SLOP002", "src/app.py", 2), ("SLOP001", "src/app.py", 3)]
    assert result.findings[0].message == (
        "Import 'fastjsonvalidatorx' is not installed or declared, and the PyPI package with "
        f"that name is new on PyPI (oldest upload 2026-10-08, 1 release): {CHECK}"
    )
    assert (result.findings[0].col, result.findings[0].subject) == (1, "fastjsonvalidatorx")
    # Without --check-imports the import is neither looked up nor reported.
    quiet = pypi("fastjsonvalidatorx", existing={"requests"})
    assert check(registry=quiet, now=NOW).findings == []
    assert quiet.queries == ["requests"]


def test_import_in_a_notebook_is_checked_with_check_imports(write, check):
    """The code cells of a notebook count like source files, under the same opt-in."""
    write("demo.ipynb", jupyter(["import os\nimport brand_new_lib\n"]))
    registry = pypi("brand-new-lib")
    result = check(registry=registry, check_imports=True, now=NOW)
    assert [(f.rule, f.subject) for f in result.findings] == [("SLOP002", "brand_new_lib")]
    assert check(registry=pypi("brand-new-lib"), now=NOW).findings == []


def test_quarantined_import_has_its_own_subject(write, check):
    """A baseline keeps a quarantine apart from an age finding, for imports too."""
    write("app.py", "import held_pkg_zz\n")
    registry = FakeFactsRegistry(set(), {"held-pkg-zz": PackageInfo(None, 0, "quarantined")})
    [finding] = check(registry=registry, check_imports=True, now=NOW).findings
    assert finding.subject == "held_pkg_zz quarantined"
    assert finding.message == (
        "Import 'held_pkg_zz' is not installed or declared, and the PyPI package with that "
        "name is quarantined on PyPI as suspected malware: remove it until PyPI clears it"
    )


def test_min_package_age_moves_the_threshold(write, check):
    """30 days by default; a team can ask for 90."""
    write("requirements.txt", "two-month-old\n")
    registry = FakeFactsRegistry(set(), {"two-month-old": uploaded(60, releases=4)})
    assert check(registry=registry, now=NOW).findings == []
    assert rule_lines(check(registry=registry, now=NOW, min_package_age=90), "SLOP002") == [1]
    assert check(registry=registry, now=NOW, min_package_age=60).findings == []
    assert rule_lines(check(registry=registry, now=NOW, min_package_age=61), "SLOP002") == [1]


def test_min_package_age_does_not_switch_the_single_release_check_off(write, check):
    """The option moves the first check only. The second always looks back a year."""
    write("requirements.txt", "left-alone\n")
    registry = FakeFactsRegistry(set(), {"left-alone": uploaded(200, releases=1)})
    result = check(registry=registry, now=NOW, min_package_age=1)
    assert "has only one release on PyPI" in result.findings[0].message
    # With the widest setting the first check covers everything the second would.
    widest = check(registry=registry, now=NOW, min_package_age=365)
    assert "is new on PyPI (oldest upload 2026-03-25, 1 release)" in widest.findings[0].message


def test_no_package_name_is_exempt_by_itself(write, check):
    """A name is no reason for silence either: slopfence reports ``slopfence`` when it is new."""
    write("requirements-dev.txt", "slopfence\npytest\n")
    result = check(registry=pypi("slopfence", existing={"pytest"}), now=NOW)
    assert findings(result) == [("SLOP002", "requirements-dev.txt", 1)]


# --- Should NOT be flagged ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "info"),
    [
        ("month-old", uploaded(30, releases=3)),  # the threshold is "less than 30 days"
        ("year-old-single", uploaded(365, releases=1)),
        ("old-single", uploaded(2000, releases=1)),
        ("young-but-active", uploaded(90, releases=2)),
        # Real dependencies of well-known projects whose names resemble a popular
        # package. A name alone is never a reason: these are forks and add-ons.
        ("httpx2", uploaded(153, releases=18)),
        ("python-backoff", uploaded(341, releases=7)),
        ("rison", uploaded(173, releases=2)),
        ("reqeusts", uploaded(200, releases=6)),
        ("no-upload-times", PackageInfo(None, 1, "active")),
        ("from-the-future", uploaded(-3, releases=1)),  # this machine's clock is behind
        ("uploaded-right-now", uploaded(0, releases=1)),
        ("before-pypi", PackageInfo(5, 1, "active")),  # a damaged cache entry
        ("negative-time", PackageInfo(-(10**12), 1, "active")),
        ("absurd-date", PackageInfo(10**18, 1, "active")),
        ("archived-pkg", uploaded(900, releases=9, status="archived")),
        ("deprecated-pkg", uploaded(100, releases=2, status="deprecated")),
        ("no-release-count", uploaded(200, releases=0)),
    ],
)
def test_established_or_unknown_packages_are_not_flagged(write, check, name, info):
    """Old enough, active enough, or not known well enough to say anything."""
    write("requirements.txt", f"{name}\n")
    write("app.py", f"import {name.replace('-', '_')}\n")
    registry = FakeFactsRegistry(set(), {name: info})
    assert check(registry=registry, check_imports=True, now=NOW).findings == []


def test_package_without_facts_is_never_flagged(write, check):
    """Established packages, v0.2 caches and pinned caches give no facts."""
    write("requirements.txt", "boto3\nrequests\n")
    assert check(registry=FakeFactsRegistry({"boto3", "requests"}, {}), now=NOW).findings == []
    assert check(registry=FakeRegistry({"boto3", "requests"}), now=NOW).findings == []


def test_facts_are_only_read_for_packages_that_exist(write, check):
    """No answer, or the answer "missing": the facts are not even asked for."""

    class Partly(FakeFactsRegistry):
        """A registry that has no answer for one package, although it holds facts for it."""

        def exists(self, name):
            """Unknown for ``no-answer-zz``; the usual answer for every other name."""
            return None if "no-answer" in normalize(name) else super().exists(name)

    write("requirements.txt", "brand-new-lib\nno-answer-zz\nghost-zz\n")
    write("src/app.py", "import no_answer_mod\nimport ghost_mod_zz\nimport new_mod_zz\n")
    registry = Partly(set(), new("brand-new-lib", "no-answer-zz", "no-answer-mod", "new-mod-zz"))
    registry.existing -= {"ghost-zz", "ghost-mod-zz"}
    result = check(registry=registry, check_imports=True, now=NOW)
    assert findings(result) == [
        ("SLOP002", "requirements.txt", 1),
        ("SLOP001", "requirements.txt", 3),
        ("SLOP001", "src/app.py", 2),
        ("SLOP002", "src/app.py", 3),
    ]
    assert registry.info_queries == ["brand-new-lib", "new_mod_zz"]
    assert result.unchecked_rules == ["SLOP001", "SLOP002"]


def test_registry_that_cannot_be_reached_never_flags(write, check):
    """If PyPI can't be reached, nothing is reported."""

    class Down(FakeFactsRegistry):
        """A registry that has facts and never gets an answer."""

        def exists(self, name):
            """Unknown."""
            return None

    write("requirements.txt", "brand-new-lib\n")
    write("src/app.py", "import other_new_lib\n")
    registry = Down(set(), new("brand-new-lib", "other-new-lib"))
    assert check(registry=registry, check_imports=True, now=NOW).findings == []
    assert registry.info_queries == []


def test_registry_without_facts_still_works(write, check):
    """A registry with only ``exists``, as before SLOP002: nothing breaks, nothing is flagged."""

    class Old:
        """A registry from before v0.3."""

        def exists(self, name):
            """Everything exists."""
            return True

    write("requirements.txt", "brand-new-lib\n")
    write("app.py", "import other_new_lib\n")
    assert check(registry=Old(), check_imports=True, now=NOW).findings == []


def test_offline_reports_nothing(write, check):
    """--offline: no lookups, no findings, no notes."""
    write("requirements.txt", "brand-new-lib\n")
    result = check(now=NOW)
    assert result.findings == [] and result.notes == []
    assert check("SLOP002", now=NOW).findings == []
    assert check(ignore=["SLOP001"], now=NOW).notes == []


def test_ignore_comments(write, check):
    """ignore[SLOP002] silences the line; a line that ignores SLOP001 is never looked up."""
    write(
        "requirements.txt",
        """
        new-one  # slopfence: ignore[SLOP002]
        new-two  # slopfence: ignore[SLOP051]
        new-three  # slopfence: ignore
        new-four  # slopfence: ignore[SLOP001]
        new-five  # slopfence: ignore[slop001, slop002]
        """,
    )
    registry = pypi("new-one", "new-two", "new-three", "new-four", "new-five")
    result = check(registry=registry, now=NOW)
    assert findings(result) == [("SLOP002", "requirements.txt", 2)]
    # A line that ignores SLOP001 (or everything) is not sent to PyPI at all, as in v0.2.
    assert registry.queries == ["new-one", "new-two"]


def test_line_ignores():
    """One helper reads the directive in every kind of file, Python source included."""
    assert line_ignores("# pinned for now") == frozenset()
    assert line_ignores("") == frozenset()
    assert line_ignores("# slopfence: ignore") == ALL_RULES == frozenset(RULES)
    assert line_ignores("#slopfence:ignore[slop002 , SLOP010,]") == {"SLOP002", "SLOP010"}
    assert line_ignores("# Slopfence: Ignore[SLOP001]  because it is ours") == {"SLOP001"}
    assert line_ignores("# slopfence: ignore-file") == frozenset()  # another directive
    assert line_ignores("# slopfence: ignore[ , ]") == frozenset()  # names no rule at all


def test_may_look_up():
    """Lookups follow SLOP001's switch, and need a running rule that could report."""
    both = PACKAGE_RULES
    assert may_look_up(frozenset().__contains__, both)
    assert may_look_up({"SLOP002"}.__contains__, both)  # SLOP001 still wants the answer
    assert not may_look_up({"SLOP001"}.__contains__, both)
    assert not may_look_up({"SLOP001"}.__contains__, {"SLOP002"})  # even when SLOP002 runs alone
    assert not may_look_up({"SLOP002"}.__contains__, {"SLOP002"})  # nothing could be reported
    assert may_look_up({"SLOP002"}.__contains__, {"SLOP001"})
    assert may_look_up({"SLOP010"}.__contains__, {"SLOP002"})


IGNORED_IN = {
    "requirements.txt": (
        "quiet-new  # slopfence: ignore[SLOP002]\nloud-new\n"
        "gone-zz  # slopfence: ignore[SLOP002]\n",
        (2, 3),
    ),
    "constraints.txt": (
        "quiet-new==1.0  # slopfence: ignore[SLOP002]\nloud-new==1.0\n"
        "gone-zz==1.0  # slopfence: ignore[SLOP002]\n",
        (2, 3),
    ),
    "pyproject.toml": (
        '[project]\nname = "demo"\ndependencies = [\n'
        '    "quiet-new",  # slopfence: ignore[SLOP002]\n    "loud-new",\n'
        '    "gone-zz",  # slopfence: ignore[SLOP002]\n]\n',
        (5, 6),
    ),
    "poetry/pyproject.toml": (
        '[tool.poetry.dependencies]\nquiet-new = "*"  # slopfence: ignore[SLOP002]\n'
        'loud-new = "*"\ngone-zz = "*"  # slopfence: ignore[SLOP002]\n',
        (3, 4),
    ),
    "Pipfile": (
        '[packages]\nquiet-new = "*"  # slopfence: ignore[SLOP002]\n'
        'loud-new = "*"\ngone-zz = "*"  # slopfence: ignore[SLOP002]\n',
        (3, 4),
    ),
    "setup.cfg": (
        "[options]\ninstall_requires =\n    quiet-new  # slopfence: ignore[SLOP002]\n"
        "    loud-new\n    gone-zz  # slopfence: ignore[SLOP002]\n",
        (4, 5),
    ),
    "setup.py": (
        "from setuptools import setup\n\nsetup(\n    install_requires=[\n"
        '        "quiet-new",  # slopfence: ignore[SLOP002]\n        "loud-new",\n'
        '        "gone-zz",  # slopfence: ignore[SLOP002]\n    ]\n)\n',
        (6, 7),
    ),
}


@pytest.mark.parametrize("rel", IGNORED_IN)
def test_ignore_slop002_in_every_dependency_format(write, check, rel):
    """``ignore[SLOP002]`` silences SLOP002 on its line in every kind of dependency file.
    The name is still looked up, and SLOP001 still reports it when it is missing."""
    content, (loud_line, gone_line) = IGNORED_IN[rel]
    write(rel, content)
    registry = pypi("quiet-new", "loud-new")
    result = check(registry=registry, now=NOW)
    assert findings(result) == [("SLOP002", rel, loud_line), ("SLOP001", rel, gone_line)]
    assert sorted(registry.queries) == ["gone-zz", "loud-new", "quiet-new"]


def test_ignore_comment_at_the_end_of_a_hashed_pin(write, check):
    """A pin with ``--hash`` lines is one requirement over several lines. pip only
    takes a comment at the end of its last line, so that is where the directive counts."""
    digest = "--hash=sha256:" + "a" * 64
    write(
        "requirements.txt",
        f"quiet-new==1.0 \\\n    {digest} \\\n    {digest}  # slopfence: ignore[SLOP002]\n"
        f"loud-new==1.0 \\\n    {digest}\n"
        f"gone-zz==1.0 \\\n    {digest}  # slopfence: ignore[SLOP002]\n"
        f"hidden-new==1.0 \\\n    {digest}  # slopfence: ignore\n"
        "# slopfence: ignore[SLOP002] \\\n"  # a comment line never goes on, also for pip
        "last-new==1.0\n",
    )
    registry = pypi("quiet-new", "loud-new", "hidden-new", "last-new")
    result = check(registry=registry, now=NOW)
    assert findings(result) == [
        ("SLOP002", "requirements.txt", 4),
        ("SLOP001", "requirements.txt", 6),
        ("SLOP002", "requirements.txt", 11),
    ]
    # A comment that covers SLOP001 keeps the name on the machine, as on one line.
    assert sorted(registry.queries) == ["gone-zz", "last-new", "loud-new", "quiet-new"]


def test_ignore_slop002_on_a_pip_line_of_a_notebook(write, check):
    """At the end of the command, or on a comment line right above it."""
    cell = (
        "!pip install quiet-new gone-zz  # slopfence: ignore[SLOP002]\n"
        "# slopfence: ignore[SLOP002]\n"
        "%pip install above-new\n"
        "!pip install loud-new\n"
    )
    write("demo.ipynb", jupyter([cell]))
    registry = pypi("quiet-new", "above-new", "loud-new")
    result = check(registry=registry, now=NOW)
    assert [(f.rule, f.subject) for f in result.findings] == [
        ("SLOP001", "gone-zz"),
        ("SLOP002", "loud-new"),
    ]
    assert registry.queries == ["quiet-new", "gone-zz", "above-new", "loud-new"]


def test_ignore_comment_that_covers_slop001_keeps_every_format_off_pypi(write, check):
    """A bare ignore, ``ignore[SLOP001]`` and ``ignore-file``: nothing is looked up, so
    SLOP002 has nothing to report."""
    write("requirements.txt", "new-a  # slopfence: ignore[SLOP001]\nnew-b  # slopfence: ignore\n")
    write("setup.cfg", "[options]\ninstall_requires =\n    new-c  # slopfence: ignore[SLOP001]\n")
    write("Pipfile", '# slopfence: ignore-file\n[packages]\nnew-d = "*"\n')
    write(
        "pyproject.toml", '[project]\nname = "x"\ndependencies = ["new-e"]  # slopfence: ignore\n'
    )
    write("demo.ipynb", jupyter(["!pip install new-f  # slopfence: ignore[SLOP001]\n"]))
    registry = pypi("new-a", "new-b", "new-c", "new-d", "new-e", "new-f")
    assert check(registry=registry, now=NOW).findings == []
    assert registry.queries == [] and registry.info_queries == []


def test_ignore_comments_on_imports(write, check):
    """On an import line, ignoring SLOP002, SLOP001 or everything silences SLOP002."""
    write(
        "src/app.py",
        """
        import newlib_a  # slopfence: ignore[SLOP002]
        import newlib_b
        import newlib_c  # slopfence: ignore[SLOP001]
        import newlib_d  # slopfence: ignore
        import ghost_e_zz  # slopfence: ignore[SLOP002]
        """,
    )
    registry = pypi("newlib-a", "newlib-b", "newlib-c", "newlib-d")
    result = check(registry=registry, check_imports=True, now=NOW)
    assert findings(result) == [("SLOP002", "src/app.py", 2), ("SLOP001", "src/app.py", 5)]
    # Where SLOP001 is ignored, the import is not looked up at all.
    asked = set(registry.queries)
    assert {"newlib_a", "newlib_b", "ghost_e_zz"} <= asked
    assert not {"newlib_c", "newlib_d", "newlib-c", "newlib-d"} & asked


def test_ignore_comments_on_notebook_imports(write, check):
    """The same in the code cell of a notebook, which the engine reads no comments of."""
    cell = (
        "import newlib_a  # slopfence: ignore[SLOP002]\n"
        "import newlib_b\n"
        "import newlib_c  # slopfence: ignore[SLOP001]\n"
        "import newlib_d  # slopfence: ignore\n"
        "import ghost_e_zz  # slopfence: ignore[SLOP002]\n"
    )
    write("demo.ipynb", jupyter([cell]))
    registry = pypi("newlib-a", "newlib-b", "newlib-c", "newlib-d")
    result = check(registry=registry, check_imports=True, now=NOW)
    assert [(f.rule, f.subject) for f in result.findings] == [
        ("SLOP002", "newlib_b"),
        ("SLOP001", "ghost_e_zz"),
    ]
    asked = set(registry.queries)
    assert {"newlib_a", "newlib_b", "ghost_e_zz"} <= asked
    assert not {"newlib_c", "newlib_d", "newlib-c", "newlib-d"} & asked


def one_line(cells):
    """A notebook saved without line breaks: the whole file is line 1."""
    return json.dumps(notebook(cells))


def one_string(cell):
    """A notebook whose code cell keeps its source as one string, so on one line."""
    doc = notebook([cell])
    doc["cells"][1]["source"] = cell
    return json.dumps(doc, indent=1)


@pytest.mark.parametrize("saved", [one_line, one_string], ids=["one line", "one string"])
@pytest.mark.parametrize("check_imports", [False, True])
def test_ignore_comment_on_a_notebook_import_covers_that_import_only(
    write, check, not_installed, saved, check_imports
):
    """A cell, or a whole notebook, can be one line of the file. A comment on one import
    then says nothing about the ``pip install`` line or the other imports on that line,
    and switching --check-imports on never takes a dependency finding away."""
    cell = (
        "%pip install held-lib brand-new-lib\n"
        "import newlib_a  # slopfence: ignore[SLOP002]\n"
        "import newlib_b\n"
    )
    write("demo.ipynb", saved([cell] if saved is one_line else cell))
    facts = new("brand-new-lib", "newlib-a", "newlib-b")
    facts["held-lib"] = uploaded(3, status="quarantined")
    registry = FakeFactsRegistry(set(), facts)
    result = check(registry=registry, check_imports=check_imports, now=NOW)
    expected = ["held-lib quarantined", "brand-new-lib"]
    assert [(f.rule, f.subject) for f in result.findings] == [
        ("SLOP002", subject) for subject in (*expected, *(["newlib_b"] if check_imports else []))
    ]
    assert len({f.line for f in result.findings}) == 1  # all on one line of the file


def test_ignored_notebook_import_is_not_looked_up_when_only_slop002_runs(
    write, check, not_installed
):
    """Nothing could report the answer. The other imports on the line are still asked about."""
    cell = "import newlib_a  # slopfence: ignore[SLOP002]\nimport newlib_b\n"
    write("demo.ipynb", one_line([cell]))
    registry = pypi("newlib-a", "newlib-b")
    result = check("SLOP002", registry=registry, check_imports=True, now=NOW)
    assert [f.subject for f in result.findings] == ["newlib_b"]
    assert set(registry.queries) == {"newlib-b", "newlib_b"}


def test_ignore_file_covers_slop002(write, check):
    """ignore-file: nothing in the file is looked up or reported."""
    write("requirements.txt", "# slopfence: ignore-file\nbrand-new-lib\n")
    write("app.py", "# slopfence: ignore-file\nimport other_new_lib\n")
    registry = pypi("brand-new-lib", "other-new-lib")
    assert check(registry=registry, check_imports=True, now=NOW).findings == []
    assert registry.queries == []


def test_known_packages_and_private_indexes_are_never_looked_up(write, check):
    """SLOP002 only sees answers to lookups SLOP001 already makes."""
    write("requirements.txt", "corp-new-lib\n")
    write("private/requirements.txt", "--index-url https://pkgs.corp.example/simple\nother-new\n")
    write("urls/requirements.txt", "third-new @ https://example.com/third.whl\n")
    write("vcs/requirements.txt", "git+https://example.com/fourth.git#egg=fourth-new\n")
    write("again/requirements.txt", "third-new\nfourth-new\n")  # a source of its own elsewhere
    names = ("corp-new-lib", "other-new", "third-new", "fourth-new")
    registry = pypi(*names)
    assert check(registry=registry, known_packages=["corp-*"], now=NOW).findings == []
    assert registry.queries == [] and registry.info_queries == []
    env = pypi(*names)
    result = check(registry=env, private_index="the PIP_INDEX_URL environment variable", now=NOW)
    assert result.findings == [] and env.queries == []
    assert result.notes[0].startswith("SLOP001 and SLOP002 made no PyPI lookups")


def test_notes_name_the_package_rules_that_run(write, check):
    """One rule running: only that one is named."""
    write("requirements.txt", "corp-new-lib\n")
    write("src/app.py", "import corp_mod\n")
    index = "the PIP_INDEX_URL environment variable"
    for rules, named in (
        ((), "SLOP001 and SLOP002"),
        (("SLOP001",), "SLOP001"),
        (("SLOP002",), "SLOP002"),
    ):
        result = check(*rules, registry=FakeRegistry(set()), private_index=index, now=NOW)
        assert result.notes[0].startswith(f"{named} made no PyPI lookups: a private")
    write("requirements.txt", "--index-url https://pkgs.corp.example/simple\ncorp-new-lib\n")
    result = check(registry=FakeRegistry(set()), check_imports=True, now=NOW)
    assert result.notes == [
        "SLOP001 and SLOP002 didn't look up imports on PyPI: requirements.txt uses a private "
        "package index, so unknown imports may be private packages."
    ]


def test_own_packages_are_never_looked_up(write, check):
    """A project that lists itself is not a new dependency: the name never leaves the machine."""
    write("pyproject.toml", '[project]\nname = "My.New_Tool"\nversion = "0.1"\n')
    write("docs/requirements.txt", "my-new-tool[docs]\nsomeone-elses-new-tool\n")
    registry = pypi("my-new-tool", "someone-elses-new-tool")
    result = check(registry=registry, now=NOW)
    assert findings(result) == [("SLOP002", "docs/requirements.txt", 2)]
    assert registry.queries == ["someone-elses-new-tool"]


def test_import_of_the_projects_own_package_is_never_looked_up(write, tmp_path, not_installed):
    """A compiled package (Rust, C) has no Python source in the tree, and pre-commit
    does not install the project. Importing it is still importing the project."""
    write("pyproject.toml", '[project]\nname = "My.New_Tool"\nversion = "0.1.1"\n')
    write("src/lib.rs", "#[pymodule]\nfn my_new_tool() {}\n")
    test = write("tests/test_api.py", "from my_new_tool import parse\nimport other_new_lib\n")
    for paths in ([tmp_path], [test]):
        registry = pypi("my-new-tool", "other-new-lib")
        result = run(paths, tmp_path, registry, check_imports=True, now=NOW)
        assert [(f.rule, f.subject) for f in result.findings] == [("SLOP002", "other_new_lib")]
        assert set(registry.queries) == {"other-new-lib", "other_new_lib"}
    # Nor is it a hallucination before its first upload.
    unpublished = FakeRegistry({"other-new-lib"})
    assert run([tmp_path], tmp_path, unpublished, check_imports=True, now=NOW).findings == []
    assert set(unpublished.queries) == {"other-new-lib", "other_new_lib"}


@pytest.mark.parametrize("select", [None, ["SLOP002"]])
def test_own_packages_are_known_whichever_files_are_checked(write, tmp_path, select):
    """A workspace: acme-a depends on the new acme-b. pre-commit passes single files."""
    write("pyproject.toml", '[tool.uv.workspace]\nmembers = ["packages/*"]\n')
    member = '[project]\nname = "acme-a"\ndependencies = ["acme-b"]\n'
    a = write("packages/a/pyproject.toml", member)
    write("packages/b/pyproject.toml", '[tool.poetry]\nname = "acme-b"\n')
    write("legacy/setup.cfg", "[metadata]\nname = acme-legacy\n")
    write("scripts/tool/setup.py", 'from setuptools import setup\nsetup(name="Acme_Tool")\n')
    docs = write("legacy/requirements-docs.txt", "acme-legacy\nacme-tool\nacme-other\n")
    names = ("acme-b", "acme-legacy", "acme-tool", "acme-other")
    for paths, expected in (
        ([tmp_path], [("SLOP002", "legacy/requirements-docs.txt", 3)]),
        ([a], []),
        ([docs], [("SLOP002", "legacy/requirements-docs.txt", 3)]),
    ):
        registry = pypi(*names)
        result = run(paths, tmp_path, registry, select=select, now=NOW)
        assert findings(result) == expected
        assert set(registry.queries) <= {"acme-other"}


def test_list_for_another_tool_stays_silent_for_both_rules(write, check):
    """A plain list in a file v0.2 did not read, with more names missing than found: it
    may be for apt or R. SLOP001 holds its findings back there, and so does SLOP002."""
    write("requirements/system.txt", "libxml2-dev\nbuild-essential\nnew-pkg\n")
    assert check(registry=pypi("new-pkg"), now=NOW).findings == []
    write("requirements/system.txt", "requests\nflask\nnew-pkg\n")
    result = check(registry=pypi("new-pkg", existing={"requests", "flask"}), now=NOW)
    assert findings(result) == [("SLOP002", "requirements/system.txt", 3)]


def test_rules_can_be_selected_separately(write, check):
    """--select SLOP001, --select SLOP002, both, neither."""
    write("requirements.txt", "ghost-dep\nbrand-new-lib\n")
    only_001 = pypi("brand-new-lib")
    assert findings(check("SLOP001", registry=only_001, now=NOW)) == [
        ("SLOP001", "requirements.txt", 1)
    ]
    assert only_001.info_queries == []  # nothing is read that the rule does not use
    alone = pypi("brand-new-lib")
    result = check("SLOP002", registry=alone, now=NOW)
    assert findings(result) == [("SLOP002", "requirements.txt", 2)] and result.notes == []
    assert alone.queries == ["ghost-dep", "brand-new-lib"]  # naming SLOP002 asks for lookups
    both = check("SLOP001", "SLOP002", registry=pypi("brand-new-lib"), now=NOW)
    assert [f.rule for f in both.findings] == ["SLOP001", "SLOP002"]
    neither = pypi("brand-new-lib")
    ignored = check(registry=neither, ignore=["SLOP001", "SLOP002"], now=NOW)
    assert ignored.findings == [] and ignored.notes == [] and neither.queries == []


def test_ignoring_slop001_still_sends_nothing(write, check):
    """v0.2 projects that ignore SLOP001 sent nothing to PyPI. They still don't."""
    write("requirements.txt", "requests\ninternal-billing-sdk\n")
    write("src/app.py", "import internal_billing\n")
    facts = new("internal-billing-sdk", "internal-billing")
    registry = FakeFactsRegistry({"requests"}, facts)
    result = check(registry=registry, ignore=["SLOP001"], check_imports=True, now=NOW)
    assert registry.queries == [] and registry.info_queries == []
    assert result.findings == []
    assert result.notes == [SLOP002_NEEDS_SLOP001]
    assert result.unchecked_rules == []
    # The same when the rules to ignore come as an iterator, which can be read once.
    result = check(registry=registry, ignore=iter(["SLOP001"]), check_imports=True, now=NOW)
    assert registry.queries == [] and result.notes == [SLOP002_NEEDS_SLOP001]
    # Naming SLOP002 in select is the way to run it without SLOP001.
    named = FakeFactsRegistry({"requests"}, facts)
    result = check("SLOP002", "SLOP010", registry=named, ignore=["SLOP001"], now=NOW)
    assert findings(result) == [("SLOP002", "requirements.txt", 2)] and result.notes == []
    # A select list from v0.2 has no SLOP002 in it: nothing changes, and no note.
    old = FakeFactsRegistry({"requests"}, facts)
    result = check("SLOP010", "SLOP051", registry=old, now=NOW)
    assert old.queries == [] and result.notes == []


def test_no_note_about_slop002_when_nothing_could_be_looked_up_anyway(write, check):
    """Offline, or with a private index in the environment, naming SLOP002 would change nothing."""
    write("requirements.txt", "internal-billing-sdk\n")
    index = "the PIP_INDEX_URL environment variable"
    registry = pypi("internal-billing-sdk")
    assert check(registry=registry, ignore=["SLOP001"], private_index=index, now=NOW).notes == []
    assert check(registry=OfflineRegistry(), ignore=["SLOP001"], now=NOW).notes == []
    assert registry.queries == []


def test_which_rules_run():
    """SLOP002 runs with SLOP001, or alone when ``select`` names it."""
    everything = set(RULES)
    assert selected_rules(None, []) == everything
    assert selected_rules(None, ["SLOP001"]) == everything - PACKAGE_RULES
    assert selected_rules([], ["SLOP001"]) == everything - PACKAGE_RULES
    assert selected_rules(None, ["SLOP002"]) == everything - {"SLOP002"}
    assert selected_rules(["SLOP002"], []) == {"SLOP002"}
    assert selected_rules(["SLOP002", "SLOP010"], ["SLOP001"]) == {"SLOP002", "SLOP010"}
    assert selected_rules(["SLOP001", "SLOP010"], []) == {"SLOP001", "SLOP010"}
    assert selected_rules(["SLOP001", "SLOP002"], ["SLOP001"]) == {"SLOP002"}
    assert slop002_has_no_lookups(None, ["SLOP001"])
    assert slop002_has_no_lookups((), ("SLOP001", "SLOP040"))
    for select, ignore in (
        (None, []),
        (None, ["SLOP002"]),
        (None, ["SLOP001", "SLOP002"]),
        (["SLOP010"], ["SLOP001"]),
        (["SLOP002"], ["SLOP001"]),
    ):
        assert not slop002_has_no_lookups(select, ignore)


def test_per_file_ignores(write, check):
    """SLOP001 off for a file: nothing of it is sent. SLOP002 off: looked up, and quiet."""
    write("requirements.txt", "new-a\n")
    write("docs/requirements.txt", "new-b\nghost-zz\n")
    write("tools/requirements.txt", "new-c\n")
    write("tools/gen.py", "import new_d\n")
    write("docs/conf.py", "import new_e\nimport ghost_mod_zz\n")
    registry = pypi("new-a", "new-b", "new-c", "new-d", "new-e")
    off = {"docs/": ["SLOP002"], "tools/": ["SLOP001"]}
    result = check(registry=registry, per_file_ignores=off, check_imports=True, now=NOW)
    assert findings(result) == [
        ("SLOP001", "docs/conf.py", 2),
        ("SLOP001", "docs/requirements.txt", 2),
        ("SLOP002", "requirements.txt", 1),
    ]
    assert not {"new-c", "new_d", "new-d"} & set(registry.queries)
    assert {"new-a", "new-b", "new_e"} <= set(registry.queries)
    # SLOP002 alone: a file where it is switched off has nothing left to look up.
    alone = pypi("new-a", "new-b", "new-c", "new-d", "new-e")
    result = check("SLOP002", registry=alone, per_file_ignores=off, check_imports=True, now=NOW)
    assert findings(result) == [("SLOP002", "requirements.txt", 1)]
    assert alone.queries == ["new-a"]


def test_a_line_no_running_rule_can_report_is_not_looked_up(write, check):
    """SLOP002 alone: a line that ignores SLOP002 has nothing left to check."""
    write("requirements.txt", "new-one  # slopfence: ignore[SLOP002]\nnew-two\n")
    write("app.py", "import new_three  # slopfence: ignore[SLOP002]\nimport new_four\n")
    write(
        "demo.ipynb", jupyter(["import new_five  # slopfence: ignore[SLOP002]\nimport new_six\n"])
    )
    registry = pypi("new-one", "new-two", "new-three", "new-four", "new-five", "new-six")
    result = check("SLOP002", registry=registry, check_imports=True, now=NOW)
    assert sorted(f.subject for f in result.findings) == ["new-two", "new_four", "new_six"]
    asked = {name.replace("_", "-") for name in registry.queries}
    assert asked == {"new-two", "new-four", "new-six"}


def test_no_lookups_when_both_package_rules_are_off(write, check):
    """Other rules never cause a lookup."""
    write("requirements.txt", "brand-new-lib\n")
    registry = pypi("brand-new-lib")
    assert check("SLOP010", registry=registry, now=NOW).findings == []
    assert registry.queries == []


def test_slop002_sends_no_name_that_slop001_would_not_send(write, tmp_path):
    """The same project with and without SLOP002: the registry is asked the same names,
    one by one and as a batch."""

    class Batched(FakeFactsRegistry):
        """A registry with facts that also takes a batch."""

        def prefetch(self, names):
            """Record the batch."""
            self.batch = list(names)

    write("requirements.txt", "requests\nnew-a\nghost-zz\ncorp-x\nnew-b  # slopfence: ignore\n")
    write("requirements-private.txt", "--extra-index-url https://pkgs.corp.example/simple\nnew-c\n")
    write("pyproject.toml", '[project]\nname = "own-new"\ndependencies = ["new-d", "own-new"]\n')
    write("setup.cfg", "[options]\ninstall_requires =\n    new-e\n")
    write("demo.ipynb", jupyter(["%pip install new-f\nimport new_g\n"]))
    write("src/app.py", "import new_h\nimport os\n")
    facts = new(*(f"new-{x}" for x in "abcdefgh"), "corp-x", "own-new")

    def asked(**options):
        """What a run asks the registry about."""
        registry = Batched({"requests"}, facts)
        run([tmp_path], tmp_path, registry, known_packages=["corp-*"], now=NOW, **options)
        assert registry.batch == list(dict.fromkeys(registry.queries))
        return registry.queries

    seen = []
    for private_file in (True, False):
        if not private_file:
            (tmp_path / "requirements-private.txt").unlink()
        for imports in (False, True):
            before = asked(select=["SLOP001"], check_imports=imports)
            assert asked(check_imports=imports) == before
            assert asked(select=["SLOP002"], check_imports=imports) == before
            seen.append(before)
    # A private index in one file covers the files v0.2 did not read, and every import.
    assert seen[0] == seen[1] == ["new-d", "requests", "new-a", "ghost-zz"]
    assert set(seen[2]) == {*seen[0], "new-e", "new-f"}
    assert set(seen[3]) - set(seen[2]) == {"new-g", "new_g", "new-h", "new_h"}


def test_diff_reports_only_changed_lines(write, check):
    """With --diff a finding needs a changed line, and other files are not looked up."""
    write("requirements.txt", "new-a\nnew-b\n")
    write("other/requirements.txt", "new-c\n")
    registry = pypi("new-a", "new-b", "new-c")
    result = check(registry=registry, changed={"requirements.txt": {2}}, now=NOW)
    assert findings(result) == [("SLOP002", "requirements.txt", 2)]
    assert "new-c" not in registry.queries


def test_severity_from_the_config_applies(write, check):
    """A team that finds High too loud can turn the rule down."""
    write("requirements.txt", "brand-new-lib\n")
    registry = pypi("brand-new-lib")
    result = check(registry=registry, severity={"SLOP002": Severity.LOW}, now=NOW)
    assert [f.severity for f in result.findings] == [Severity.LOW]


def test_baseline_does_not_hide_a_later_quarantine(write, check):
    """A finding that was accepted while the package was new comes back when PyPI
    quarantines it. Its second release, or its age, changes nothing about the entry."""
    write("requirements.txt", "brand-new-lib\n")
    first = check(registry=pypi("brand-new-lib"), now=NOW)
    accepted = Baseline({key_of(f): 1 for f in first.findings})
    assert list(accepted.entries) == [("requirements.txt", "SLOP002", "brand-new-lib")]

    def later(info, days):
        """The run ``days`` later, with the baseline, when PyPI says ``info``."""
        registry = FakeFactsRegistry(set(), {"brand-new-lib": info})
        return check(registry=registry, baseline=accepted, now=NOW + days * DAY)

    still_new = later(uploaded(3, releases=1), 5)
    assert still_new.findings == [] and len(still_new.baselined) == 1
    single_release = later(uploaded(3, releases=1), 60)  # the same concern, another wording
    assert (
        single_release.findings == [] and "only one release" in single_release.baselined[0].message
    )
    held = later(uploaded(3, releases=1, status="quarantined"), 60)
    assert [f.subject for f in held.findings] == ["brand-new-lib quarantined"]
    assert held.baselined == []
    # A year on there is no finding left. The entry is not called fixed: an answer
    # from PyPI that is missing must never look like a fix.
    aged = later(uploaded(3, releases=1), 400)
    assert aged.findings == [] and aged.baselined == [] and aged.notes == []


def test_one_request_per_package_and_one_finding_per_line(write, tmp_path, monkeypatch):
    """The same new package in three places: PyPI is asked once, each place is reported."""
    write("requirements.txt", "brand-new-lib\nBrand_New.Lib>=2\n")
    write("docs/requirements.txt", "brand-new-lib\n")
    asked = []

    def query(self, key):
        """Answer like PyPI for a package that is three days old."""
        asked.append(key)
        return Lookup(True, uploaded(3, releases=2))

    monkeypatch.setattr(PyPIRegistry, "_query", query)
    result = run([tmp_path], tmp_path, PyPIRegistry(tmp_path / "cache", now=NOW), now=NOW)
    assert findings(result) == [
        ("SLOP002", "docs/requirements.txt", 1),
        ("SLOP002", "requirements.txt", 1),
        ("SLOP002", "requirements.txt", 2),
    ]
    assert asked == ["brand-new-lib"]


# --- Time ----------------------------------------------------------------------------------


def test_message_and_verdict_are_the_same_all_day():
    """The text names the upload date, not an age, and days are UTC calendar days."""
    midnight = NOW - 12 * 3600  # 2026-10-11 00:00:00 UTC
    info = PackageInfo(midnight - 29 * DAY + 5, 2)  # uploaded 2026-09-12 00:00:05 UTC
    morning = assess(info, midnight + 60)
    evening = assess(info, midnight + DAY - 1)
    assert morning == evening and morning.kind == NEW
    assert morning.text == f"is new on PyPI (oldest upload 2026-09-12, 2 releases): {CHECK}"
    assert "ago" not in morning.text
    # The next day it is 30 days old and no longer reported.
    assert assess(info, midnight + DAY) is None
    # Uploaded a second before midnight: one calendar day more, whatever the hour is.
    late = PackageInfo(midnight - 29 * DAY - 1, 2)  # 2026-09-11 23:59:59 UTC
    assert assess(late, midnight) is None and assess(late, midnight - 1).kind == NEW
    assert assess(None, NOW) is None


def test_kinds_of_risk():
    """The kind tells a quarantine from an age finding (baselines keep them apart)."""
    new_pkg, single, held = (
        assess(uploaded(3, 5), NOW),
        assess(uploaded(100, 1), NOW),
        assess(uploaded(3, 1, status="quarantined"), NOW),
    )
    assert (new_pkg.kind, single.kind, held.kind) == (NEW, SINGLE_RELEASE, QUARANTINED)
    assert [risk.subject("pkg") for risk in (new_pkg, single, held)] == [
        "pkg",
        "pkg",
        "pkg quarantined",
    ]
    assert not any("malicious" in risk.text for risk in (new_pkg, single, held))
    assert assess(uploaded(3, 5), NOW)[1] == new_pkg.text  # still a (kind, text) pair


def test_release_count_is_left_out_when_unknown():
    """PyPI's listing didn't say how many versions there are."""
    risk = assess(PackageInfo(NOW - 2 * DAY, 0), NOW)
    assert risk.kind == NEW and risk.text == f"is new on PyPI (oldest upload 2026-10-09): {CHECK}"
    assert assess(PackageInfo(NOW - 200 * DAY, 0), NOW) is None


def test_default_now_is_the_clock(write, check):
    """Without ``now``, today is today."""
    write("requirements.txt", "brand-new-lib\n")
    facts = {"brand-new-lib": PackageInfo(int(time.time()) - 3 * DAY, 1)}
    assert rule_lines(check(registry=FakeFactsRegistry(set(), facts)), "SLOP002") == [1]


# --- Command line and config -------------------------------------------------------------------


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    """A project whose PyPI answers, with facts, are pinned in the cache file:
    (root, upload date, names the registry was asked about)."""
    (tmp_path / "requirements.txt").write_text("requests\nbrand-new-lib\n", encoding="utf-8")
    cache = tmp_path / "cache" / "slopfence"
    cache.mkdir(parents=True)
    first = int(time.time()) - 5 * DAY
    (cache / "pypi.json").write_text(
        json.dumps(
            {
                "requests": [True, 9e12],
                "brand-new-lib": [True, 9e12],
                "_meta": {"brand-new-lib": [first, 1, "active", 9e12]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SLOPFENCE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)

    asked: list[str] = []
    real_exists = PyPIRegistry.exists

    def exists(self, name):
        """Record every name slopfence asks the registry about."""
        asked.append(name)
        return real_exists(self, name)

    monkeypatch.setattr(PyPIRegistry, "exists", exists)
    monkeypatch.setattr(PyPIRegistry, "_query", lambda self, key: pytest.fail(f"lookup of {key}"))
    return tmp_path, time.strftime("%Y-%m-%d", time.gmtime(first)), asked


def test_cli_reports_slop002_in_every_format(pinned, capsys):
    """Text, JSON and SARIF, each with exit code 1."""
    _, date, _ = pinned
    message = (
        f"Dependency 'brand-new-lib' is new on PyPI (oldest upload {date}, 1 release): {CHECK}"
    )
    assert main([".", "--no-color"]) == 1
    out = capsys.readouterr().out
    assert f"  2:1      SLOP002  {message}  [high]\n" in out
    assert out.rstrip().endswith("1 issue (1 high)")

    assert main([".", "--format", "json"]) == 1
    [finding] = json.loads(capsys.readouterr().out)["findings"]
    assert finding == {
        "rule": "SLOP002",
        "name": "slopsquatting-risk",
        "severity": "high",
        "path": "requirements.txt",
        "line": 2,
        "column": 1,
        "message": message,
    }

    assert main([".", "--format", "sarif"]) == 1
    sarif = json.loads(capsys.readouterr().out)["runs"][0]
    [result] = sarif["results"]
    assert (result["ruleId"], result["level"], result["message"]["text"]) == (
        "SLOP002",
        "error",
        message,
    )
    [rule] = [rule for rule in sarif["tool"]["driver"]["rules"] if rule["id"] == "SLOP002"]
    assert (rule["name"], rule["defaultConfiguration"]["level"]) == ("slopsquatting-risk", "error")


def test_cli_options_that_silence_slop002(pinned, capsys):
    """--ignore, --offline, --known-packages; and what min-package-age does not switch off."""
    root, _, _ = pinned
    assert main([".", "--fail-on", "high"]) == 1
    assert main([".", "--ignore", "SLOP002"]) == 0
    assert main([".", "--offline"]) == 0
    assert main([".", "--known-packages", "brand-new-*"]) == 0
    assert main([".", "--exit-zero"]) == 0
    capsys.readouterr()
    assert main([".", "--min-package-age", "3"]) == 1  # still flagged: it has a single release
    assert "has only one release on PyPI" in capsys.readouterr().out
    (root / "pyproject.toml").write_text(
        "[tool.slopfence]\nmin-package-age = 2\n", encoding="utf-8"
    )
    assert main(["."]) == 1
    assert "has only one release on PyPI" in capsys.readouterr().out
    assert main([".", "--min-package-age", "90"]) == 1  # the option replaces the config value
    assert "is new on PyPI" in capsys.readouterr().out
    (root / "requirements.txt").write_text(
        "requests\nbrand-new-lib  # slopfence: ignore[SLOP002]\n", encoding="utf-8"
    )
    assert main(["."]) == 0


def test_list_rules_shows_slop002(tmp_path, capsys, monkeypatch):
    """The rule is listed with its severity, after SLOP001."""
    monkeypatch.chdir(tmp_path)
    assert main(["--list-rules"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == (
        "SLOP002  high    slopsquatting-risk: Dependency or import whose PyPI package is "
        "new, has a single release or is quarantined (slopsquatting risk)."
    )


def test_cli_ignoring_slop001_sends_nothing_and_says_so(pinned, capsys):
    """--ignore SLOP001 (or the config key) keeps its v0.2 meaning: no lookups."""
    root, _, asked = pinned
    assert main([".", "--ignore", "SLOP001", "--no-color"]) == 0
    assert asked == []
    assert capsys.readouterr().out.endswith(f"note: {SLOP002_NEEDS_SLOP001}\n")
    (root / "pyproject.toml").write_text(
        '[tool.slopfence]\nignore = ["SLOP001"]\n', encoding="utf-8"
    )
    assert main([".", "--format", "json"]) == 0
    assert asked == [] and json.loads(capsys.readouterr().out)["notes"] == [SLOP002_NEEDS_SLOP001]
    assert main([".", "--offline"]) == 0  # offline says nothing about it
    assert "note:" not in capsys.readouterr().out
    assert main([".", "--select", "SLOP002"]) == 1
    assert asked == ["requests", "brand-new-lib"]


def test_cli_with_a_cache_pinned_the_v02_way_asks_and_reports_nothing(pinned, capsys):
    """CI and users pin ``{name: [found, time]}``. Such a file answers by itself, and
    without facts there is no SLOP002 finding."""
    root, _, asked = pinned
    # One entry stamped as a v0.2 run an hour ago would stamp it, the others pinned for good.
    an_hour_ago = int(time.time()) - 3600
    v02 = {
        "requests": [True, an_hour_ago],
        "brand-new-lib": [True, 9e12],
        "ghost-zz": [False, 9e12],
    }
    cache = root / "cache" / "slopfence" / "pypi.json"
    cache.write_text(json.dumps(v02), encoding="utf-8")
    (root / "requirements.txt").write_text("requests\nbrand-new-lib\nghost-zz\n", encoding="utf-8")
    assert main([".", "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert [(f["rule"], f["line"]) for f in report["findings"]] == [("SLOP001", 3)]
    assert asked == ["requests", "brand-new-lib", "ghost-zz"]
    assert json.loads(cache.read_text(encoding="utf-8")) == v02  # and the file is left alone


def test_cli_reads_the_clock_once_for_the_cache_and_the_rule(pinned, capsys, monkeypatch):
    """One time for the whole run: the cache and SLOP002 can't disagree about today."""
    root, _, _ = pinned
    cache = root / "cache" / "slopfence" / "pypi.json"
    facts = {"brand-new-lib": [NOW - 29 * DAY, 4, "active", NOW - 3600]}
    cache.write_text(
        json.dumps({"requests": [True, NOW], "brand-new-lib": [True, NOW], "_meta": facts}),
        encoding="utf-8",
    )
    looks = []

    def clock():
        """Count the looks at the clock, and answer ``NOW``."""
        looks.append(1)
        return NOW

    monkeypatch.setattr(cli, "time", SimpleNamespace(time=clock))
    assert main([".", "--format", "json"]) == 1
    [finding] = json.loads(capsys.readouterr().out)["findings"]
    assert "(oldest upload 2026-09-12, 4 releases)" in finding["message"]
    assert len(looks) == 1
    # A day later the package is 30 days old. The facts are a day old too, so PyPI
    # would be asked again: the run stops at the lookup this fixture forbids.
    monkeypatch.setattr(cli, "time", SimpleNamespace(time=lambda: NOW + DAY))
    with pytest.raises(pytest.fail.Exception, match="lookup of brand-new-lib"):
        main([".", "--format", "json"])


def test_cli_gives_the_same_bytes_for_the_same_cache_on_the_same_day(pinned, capsys):
    """Nothing in a report depends on the time of day."""
    reports = []
    for fmt in ("text", "json", "sarif") * 2:
        main([".", "--format", fmt, "--no-color"])
        reports.append(capsys.readouterr().out)
    assert reports[:3] == reports[3:]


@pytest.mark.parametrize("value", ["0", "-5", "366", "soon", "2.5", "", "1e2"])
def test_cli_rejects_bad_min_package_age(tmp_path, value, capsys):
    """Whole days from 1 to 365; anything else is a usage error."""
    with pytest.raises(SystemExit) as err:
        main([str(tmp_path), "--offline", "--min-package-age", value])
    assert err.value.code == 2
    assert (
        f"argument --min-package-age: {value!r} is not a whole number of days from 1 to 365"
        in capsys.readouterr().err
    )


@pytest.mark.parametrize("value", ["0", "366", '"30"', "true", "30.5", "[30]"])
def test_config_rejects_bad_min_package_age(tmp_path, value, capsys):
    """The config key takes the same values as the option."""
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.slopfence]\nmin-package-age = {value}\n", encoding="utf-8"
    )
    wanted = r"\[tool.slopfence\]: 'min-package-age' must be a whole number of days from 1 to 365$"
    with pytest.raises(ConfigError, match=wanted):
        load_config(tmp_path)
    assert main([str(tmp_path), "--offline"]) == 2
    assert "slopfence: error:" in capsys.readouterr().err


def test_config_min_package_age(tmp_path):
    """30 unless the project says otherwise; 1 and 365 are the ends."""
    assert load_config(tmp_path).min_package_age == 30
    for days in (1, 365):
        (tmp_path / "pyproject.toml").write_text(
            f"[tool.slopfence]\nmin-package-age = {days}\n", encoding="utf-8"
        )
        assert load_config(tmp_path).min_package_age == days
