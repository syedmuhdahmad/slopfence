"""Per-file ignores and per-rule severity overrides (#52)."""

import json

import pytest

from slopfence import reporters
from slopfence.cli import main
from slopfence.config import ConfigError, ignored_rules, load_config
from slopfence.models import RULES, Finding, Severity
from tests.conftest import FakeBatchRegistry, git, pyproject

PLACEHOLDER = "# In a real implementation, retry\n"  # SLOP010, medium
CHAT = "# I hope this helps!\n"  # SLOP051, medium
HELPER = """
def {name}(value, fmt="%Y"):
    if value is None:
        return ""
    text = value.strftime(fmt)
    return text.strip()
"""
# SLOP040, low
SWALLOW = """
def load(path):
    try:
        return open(path).read()
    except Exception:
        pass
"""


def reported(capsys, *args):
    """Run the command line with JSON output: (path, rule, severity) of each finding."""
    capsys.readouterr()
    main([*args, "--offline", "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    return [(f["path"], f["rule"], f["severity"]) for f in report["findings"]]


# --- per-file-ignores: the config -------------------------------------------------------


def test_per_file_ignores_are_loaded(tmp_path):
    """Patterns keep their spelling; rule IDs are upper-cased, as in select and ignore."""
    pyproject(
        tmp_path,
        '[tool.slopfence.per-file-ignores]\n"scripts/*" = ["slop040"]\n'
        'legacy = ["SLOP030", "SLOP011"]\n"empty.py" = []\n',
    )
    assert load_config(tmp_path).per_file_ignores == {
        "scripts/*": ["SLOP040"],
        "legacy": ["SLOP030", "SLOP011"],
        "empty.py": [],
    }
    assert load_config(tmp_path / "missing").per_file_ignores == {}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            '[tool.slopfence]\nper-file-ignores = ["SLOP040"]\n',
            "'per-file-ignores' must be a table of pattern = [rule IDs]",
        ),
        (
            '[tool.slopfence.per-file-ignores]\n"scripts/*" = "SLOP040"\n',
            "'per-file-ignores': the value for \"scripts/*\" must be a list of rule IDs",
        ),
        (
            '[tool.slopfence.per-file-ignores]\n"scripts/*" = [40]\n',
            "'per-file-ignores': the value for \"scripts/*\" must be a list of rule IDs",
        ),
        (
            # TOML reads an unquoted key with a dot as a table inside a table.
            '[tool.slopfence.per-file-ignores]\nconftest.py = ["SLOP010"]\n',
            "'per-file-ignores': the value for \"conftest\" must be a list of rule IDs "
            "(put quotes around a pattern that contains a dot)",
        ),
        (
            '[tool.slopfence.per-file-ignores]\n"scripts/*" = ["SLOP999"]\n',
            "unknown rule(s) in 'per-file-ignores.\"scripts/*\"': SLOP999",
        ),
        (
            '[tool.slopfence.per-file-ignores]\n"./" = ["SLOP010"]\n',
            "'per-file-ignores' has an empty pattern",
        ),
    ],
)
def test_invalid_per_file_ignores(tmp_path, body, message):
    """Bad values, unknown rule IDs and a pattern that can match nothing are config errors."""
    pyproject(tmp_path, body)
    with pytest.raises(ConfigError) as err:
        load_config(tmp_path)
    assert str(err.value).endswith(f"[tool.slopfence]: {message}")


def test_invalid_per_file_ignores_exit_2(tmp_path, monkeypatch, capsys):
    """The command line reports the config error and exits 2."""
    pyproject(tmp_path, '[tool.slopfence.per-file-ignores]\n"a.py" = ["SLOP999"]\n')
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline"]) == 2
    assert "unknown rule(s) in 'per-file-ignores.\"a.py\"': SLOP999" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("scripts/run.py", {"SLOP040"}),
        ("scripts/sub/run.py", {"SLOP040"}),  # "*" also matches "/"
        ("tools/scripts/run.py", set()),  # a pattern with "/" is anchored at the root
        ("legacy/a.py", {"SLOP030", "SLOP011"}),
        ("src/legacy/a.py", {"SLOP030", "SLOP011"}),  # no "/" in the middle: any folder
        ("gen/x_pb2.py", {"SLOP010"}),
        ("legacy/x_pb2.py", {"SLOP030", "SLOP011", "SLOP010"}),  # matching patterns add up
        ("src/app.py", set()),
    ],
)
def test_ignored_rules_use_exclude_patterns(path, expected):
    """A pattern matches the files that the same ``exclude`` pattern would skip."""
    table = {"scripts/*": ["SLOP040"], "legacy/": ["SLOP030", "SLOP011"], "*_pb2.py": ["SLOP010"]}
    assert ignored_rules(path, table) == expected


# --- per-file-ignores: what a run reports -----------------------------------------------


def test_rule_is_off_only_in_matching_files(write, tmp_path, monkeypatch, capsys):
    """The rule is skipped in matching files. Other rules and other files are not."""
    write("scripts/run.py", PLACEHOLDER + CHAT)
    write("src/app.py", PLACEHOLDER + CHAT)
    pyproject(tmp_path, '[tool.slopfence.per-file-ignores]\n"scripts/*" = ["SLOP010"]\n')
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline"]) == 1
    assert [f[:2] for f in reported(capsys, ".")] == [
        ("scripts/run.py", "SLOP051"),
        ("src/app.py", "SLOP010"),
        ("src/app.py", "SLOP051"),
    ]
    # --ignore still applies on top.
    assert [f[:2] for f in reported(capsys, ".", "--ignore", "SLOP051")] == [
        ("src/app.py", "SLOP010")
    ]
    # A file passed by name, as pre-commit does, is matched by its path in the project.
    assert [f[:2] for f in reported(capsys, "scripts/run.py")] == [("scripts/run.py", "SLOP051")]


def test_per_file_ignore_can_leave_a_file_without_findings(write, check):
    """With every rule a file breaks switched off for it, the run finds nothing there."""
    write("scripts/run.py", PLACEHOLDER + CHAT + SWALLOW)
    ignores = {"scripts/": ["SLOP010", "SLOP051", "SLOP040"]}
    assert len(check().findings) == 3
    result = check(per_file_ignores=ignores)
    assert (result.findings, result.files_checked) == ([], 1)


def test_per_file_ignore_for_a_rule_that_is_not_selected_changes_nothing(write, check):
    """A pattern may name a rule that this run doesn't use."""
    write("app.py", PLACEHOLDER + CHAT)
    result = check("SLOP051", per_file_ignores={"app.py": ["SLOP010"]})
    assert [f.rule for f in result.findings] == ["SLOP051"]


def test_slop001_is_not_looked_up_in_ignored_files(write, check):
    """Dependencies and imports of a file where SLOP001 is switched off are never sent."""
    write("requirements.txt", "made-up-public-zz\n")
    write("requirements-internal.txt", "corp-secret-zz\n")
    write("src/app.py", "import made_up_import_zz\n")
    write("src/internal.py", "import corp_secret_import_zz\n")
    # This registry also takes a batch of names up front, like the real one. The names
    # of an ignored file must be in neither the batch nor a single question.
    registry = FakeBatchRegistry(set())
    result = check(
        "SLOP001",
        registry=registry,
        check_imports=True,
        per_file_ignores={"requirements-internal.txt": ["SLOP001"], "internal.py": ["SLOP001"]},
    )
    assert [(f.path, f.line) for f in result.findings] == [
        ("requirements.txt", 1),
        ("src/app.py", 1),
    ]
    asked = " ".join([*registry.queries, *(name for batch in registry.batches for name in batch)])
    assert "made-up-public-zz" in asked and "made_up_import_zz" in asked
    assert "corp" not in asked
    assert len(registry.batches) == 1  # the batch was really used


def test_imports_under_an_ignore_comment_are_not_looked_up(write, check):
    """An ignore comment that covers SLOP001 hides the finding, so the name is not sent
    either: not for a whole file, and not for one line. Another rule's comment changes nothing."""
    write("src/skipped.py", "# slopfence: ignore-file\nimport corp_file_zz\n")
    write(
        "src/app.py",
        "import corp_line_zz  # slopfence: ignore\n"
        "import corp_rule_zz  # slopfence: ignore[SLOP001]\n"
        "import made_up_zz  # slopfence: ignore[SLOP010]\n"
        "import shared_zz  # slopfence: ignore\n",
    )
    write("src/other.py", "import shared_zz\n")  # the same module, without a comment
    registry = FakeBatchRegistry(set())
    result = check("SLOP001", registry=registry, check_imports=True)
    assert [(f.path, f.line) for f in result.findings] == [("src/app.py", 3), ("src/other.py", 1)]
    asked = {*registry.queries, *(name for batch in registry.batches for name in batch)}
    assert asked == {"made_up_zz", "made-up-zz", "shared_zz", "shared-zz"}


def test_dependencies_of_an_ignored_file_still_count_as_declared(write, check):
    """Switching SLOP001 off for pyproject.toml stops its lookups, not what it declares."""
    write("pyproject.toml", '[project]\nname = "demo"\ndependencies = ["corp-auth-zz"]\n')
    write("app.py", "import corp_auth_zz\nimport made_up_import_zz\n")
    registry = FakeBatchRegistry(set())
    result = check(
        "SLOP001",
        registry=registry,
        check_imports=True,
        per_file_ignores={"pyproject.toml": ["SLOP001"]},
    )
    # corp_auth_zz is provided by a declared dependency, so only the other import is asked.
    assert [(f.path, f.line) for f in result.findings] == [("app.py", 2)]
    assert "corp" not in " ".join(registry.queries + registry.batches[0])


@pytest.mark.parametrize(
    ("ignored", "new"),
    [("legacy", "src"), ("vendor", "app"), ("third_party", "core"), ("scripts", "pkg")],
)
def test_slop030_is_off_for_functions_in_matching_files(write, check, ignored, new):
    """A copy inside the ignored folder is not reported; code that copies from it is.

    Whichever folder sorts first: a file where SLOP030 is off never holds the copy
    that is reported, and it never makes a copy elsewhere "the first one".
    """
    write(f"{ignored}/dates.py", HELPER.format(name="format_date"))
    write(f"{ignored}/old_copy.py", HELPER.format(name="fmt_date"))
    write(f"{new}/new_copy.py", HELPER.format(name="date_formatter"))
    result = check("SLOP030", per_file_ignores={f"{ignored}/": ["SLOP030"]})
    assert [(f.path, f.rule) for f in result.findings] == [(f"{new}/new_copy.py", "SLOP030")]
    assert f"'format_date' in {ignored}/dates.py:1" in result.findings[0].message


def test_every_copy_of_ignored_code_is_reported(write, check):
    """Three copies of a vendored helper: all three are reported, each pointing at it."""
    for rel, name in [("app/x.py", "fx"), ("app/y.py", "fy"), ("app/z.py", "fz")]:
        write(rel, HELPER.format(name=name))
    write("vendor/a.py", HELPER.format(name="fa"))
    result = check("SLOP030", per_file_ignores={"vendor/": ["SLOP030"]})
    assert [f.path for f in result.findings] == ["app/x.py", "app/y.py", "app/z.py"]
    assert all("'fa' in vendor/a.py:1" in f.message for f in result.findings)
    # Without the per-file ignore, the first copy by path is the one left out.
    assert [f.path for f in check("SLOP030").findings] == ["app/y.py", "app/z.py", "vendor/a.py"]


def test_copy_added_to_an_ignored_file_passes_a_diff_run(write, check):
    """With --diff a copy is only reported when its own lines changed.

    A change that adds a copy of outside code to the ignored folder touches no other
    file. The full run reports the outside code, which is older. The --diff run has
    no changed line to report it on: a known limit, said in the README.
    """
    write("app/dates.py", HELPER.format(name="format_date"))
    write("vendor/lib.py", HELPER.format(name="vendored_format"))
    ignores = {"vendor/": ["SLOP030"]}
    full = check("SLOP030", per_file_ignores=ignores)
    assert [(f.path, f.line) for f in full.findings] == [("app/dates.py", 1)]
    assert "'vendored_format' in vendor/lib.py:1" in full.findings[0].message
    added_inside = check("SLOP030", per_file_ignores=ignores, changed={"vendor/lib.py": None})
    assert added_inside.findings == []
    # The other way round, the new code is outside the folder, and both runs report it.
    added_outside = check("SLOP030", per_file_ignores=ignores, changed={"app/dates.py": None})
    assert added_outside.findings == full.findings


def test_slop030_ignore_does_not_hide_copies_among_other_files(write, check):
    """Two copies outside the ignored folder, none inside: reported as usual."""
    write("app/x.py", HELPER.format(name="fx"))
    write("app/y.py", HELPER.format(name="fy"))
    write("vendor/other.py", "def unrelated():\n    return 1\n")
    result = check("SLOP030", per_file_ignores={"vendor/": ["SLOP030"]})
    assert [f.path for f in result.findings] == ["app/y.py"]


# --- severity: the config ---------------------------------------------------------------


def test_severity_is_loaded(tmp_path):
    """Rule IDs are case-insensitive, like everywhere else."""
    pyproject(tmp_path, '[tool.slopfence.severity]\nslop040 = "medium"\nSLOP001 = "low"\n')
    assert load_config(tmp_path).severity == {
        "SLOP040": Severity.MEDIUM,
        "SLOP001": Severity.LOW,
    }
    assert load_config(tmp_path / "missing").severity == {}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            '[tool.slopfence]\nseverity = "high"\n',
            '\'severity\' must be a table of rule ID = "high", "medium" or "low"',
        ),
        ('[tool.slopfence.severity]\nSLOP999 = "high"\n', "unknown rule(s) in 'severity': SLOP999"),
        (
            '[tool.slopfence.severity]\nSLOP040 = "critical"\n',
            "'severity': SLOP040 must be one of high, medium, low",
        ),
        (
            '[tool.slopfence.severity]\nSLOP040 = "High"\n',
            "'severity': SLOP040 must be one of high, medium, low",
        ),
        (
            "[tool.slopfence.severity]\nSLOP040 = 3\n",
            "'severity': SLOP040 must be one of high, medium, low",
        ),
        (
            '[tool.slopfence.severity]\nSLOP040 = "high"\nslop040 = "low"\n',
            "'severity' sets SLOP040 more than once",
        ),
    ],
)
def test_invalid_severity(tmp_path, body, message):
    """Unknown rule IDs and unknown severities are config errors."""
    pyproject(tmp_path, body)
    with pytest.raises(ConfigError) as err:
        load_config(tmp_path)
    assert str(err.value).endswith(f"[tool.slopfence]: {message}")


def test_invalid_severity_exits_2(tmp_path, monkeypatch, capsys):
    """The command line reports the config error and exits 2."""
    pyproject(tmp_path, '[tool.slopfence.severity]\nSLOP040 = "critical"\n')
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline"]) == 2
    assert "'severity': SLOP040 must be one of high, medium, low" in capsys.readouterr().err


def test_list_rules_with_an_invalid_config_exits_2(tmp_path, monkeypatch, capsys):
    """--list-rules reads the config, so a config that is wrong is an error there too."""
    pyproject(tmp_path, '[tool.slopfence.severity]\nSLOP040 = "critical"\n')
    monkeypatch.chdir(tmp_path)
    assert main(["--list-rules"]) == 2
    captured = capsys.readouterr()
    assert "'severity': SLOP040 must be one of high, medium, low" in captured.err
    assert captured.out == ""


# --- severity: what a run reports -------------------------------------------------------


def test_overridden_severity_shows_in_every_format(write, tmp_path, monkeypatch, capsys):
    """Text, JSON and SARIF all show the severity the config sets."""
    write("app.py", SWALLOW + CHAT)
    pyproject(tmp_path, '[tool.slopfence.severity]\nSLOP040 = "high"\nSLOP051 = "low"\n')
    monkeypatch.chdir(tmp_path)

    main([".", "--offline", "--no-color"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith("  4:5      SLOP040  ") and lines[1].endswith("  [high]")
    assert lines[2].startswith("  6:1      SLOP051  ") and lines[2].endswith("  [low]")
    assert lines[-1] == "2 issues (1 high, 1 low)"

    assert reported(capsys, ".") == [("app.py", "SLOP040", "high"), ("app.py", "SLOP051", "low")]

    main([".", "--offline", "--format", "sarif"])
    sarif = json.loads(capsys.readouterr().out)["runs"][0]
    assert {r["ruleId"]: r["level"] for r in sarif["results"]} == {
        "SLOP040": "error",
        "SLOP051": "note",
    }
    # The rule's own level follows too, and a rule without an override keeps its level.
    levels = {r["id"]: r["defaultConfiguration"]["level"] for r in sarif["tool"]["driver"]["rules"]}
    assert (levels["SLOP040"], levels["SLOP051"], levels["SLOP010"]) == ("error", "note", "warning")


def test_overridden_severity_shows_in_colour(write, check):
    """The colour of a finding follows the severity the config sets, like its label."""
    write("app.py", SWALLOW)
    plain = reporters.text(check(), color=True)
    red = reporters.text(check(severity={"SLOP040": Severity.HIGH}), color=True)
    assert "\x1b[36mlow\x1b[0m" in plain and "\x1b[31m" not in plain
    assert "\x1b[31mhigh\x1b[0m" in red and "\x1b[36m" not in red


def test_overridden_severity_drives_fail_on(write, tmp_path, monkeypatch):
    """--fail-on compares with the severity the config sets."""
    write("app.py", SWALLOW)
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline", "--fail-on", "high"]) == 0  # SLOP040 is low by default
    pyproject(tmp_path, '[tool.slopfence.severity]\nSLOP040 = "high"\n')
    assert main([".", "--offline", "--fail-on", "high"]) == 1
    write("app.py", CHAT)  # SLOP051 is medium by default
    assert main([".", "--offline", "--fail-on", "medium"]) == 1
    pyproject(tmp_path, '[tool.slopfence.severity]\nSLOP051 = "low"\n')
    assert main([".", "--offline", "--fail-on", "medium"]) == 0
    assert main([".", "--offline"]) == 1  # still reported, and it fails at the default "low"


def test_overrides_never_change_the_shared_rules_table(write, check):
    """An override lives on the findings of one run. RULES and later runs keep the defaults."""
    write("app.py", SWALLOW)
    result = check(severity={"SLOP040": Severity.HIGH})
    [finding] = result.findings
    assert finding.severity is Severity.HIGH
    assert result.severities == {"SLOP040": Severity.HIGH}
    assert RULES["SLOP040"].severity is Severity.LOW
    assert Finding("SLOP040", "app.py", 1, 1, "x").severity is Severity.LOW
    later = check()
    [again] = later.findings
    assert again.severity is Severity.LOW and again.severity_override is None
    assert later.severities == {}
    # An override equal to the rule's own severity leaves the finding as it is.
    assert check(severity={"SLOP040": Severity.LOW}).findings == [again]


def test_list_rules_shows_configured_severities(tmp_path, monkeypatch, capsys):
    """--list-rules shows the severity this project uses, and the rule's own next to it."""
    monkeypatch.chdir(tmp_path)
    assert main(["--list-rules"]) == 0
    plain = capsys.readouterr().out
    assert "SLOP040  low     swallowed-exception:" in plain and "(default:" not in plain
    assert len(plain.splitlines()) == len(RULES)
    pyproject(tmp_path, '[tool.slopfence.severity]\nSLOP040 = "medium"\nSLOP001 = "high"\n')
    assert main(["--list-rules"]) == 0
    lines = capsys.readouterr().out.splitlines()
    [slop040] = [line for line in lines if line.startswith("SLOP040")]
    assert slop040.startswith("SLOP040  medium  swallowed-exception:")
    assert slop040.endswith("(default: low)")
    # Set to its own severity: there is nothing to point out.
    [slop001] = [line for line in lines if line.startswith("SLOP001")]
    assert slop001 in plain


def test_list_rules_does_not_need_the_paths_to_exist(tmp_path, monkeypatch, capsys):
    """The listing checks no file, so a path that isn't there is no error (as in v0.2)."""
    pyproject(tmp_path, '[tool.slopfence.severity]\nSLOP040 = "medium"\n')
    (tmp_path / "src").mkdir()
    git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path / "src")
    assert main(["no-such-folder/deeper/x.py", "--list-rules"]) == 0
    # The project is found from the nearest folder that exists: here, the repository
    # above src/, where the config is.
    assert "SLOP040  medium" in capsys.readouterr().out
    # Any other run still says that the path does not exist.
    assert main(["no-such-folder/deeper/x.py", "--offline"]) == 2
    assert "no-such-folder" in capsys.readouterr().err
