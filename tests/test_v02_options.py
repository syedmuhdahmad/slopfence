"""known-packages (#15), --fail-on (#18), --strict (#19)."""

import json

import pytest

from slopfence.cli import main
from slopfence.config import ConfigError, load_config
from slopfence.engine import run
from tests.conftest import FakeRegistry, rule_lines
from tests.conftest import pyproject as _pyproject

MEDIUM_ONLY = "# I hope this helps!\n"  # SLOP051 is medium
BROKEN = "def broken(:\n"


# --- known-packages (#15) ---------------------------------------------------


def test_known_packages_skip_dependency_and_import_checks(write, tmp_path):
    """known-packages are accepted without a lookup, in dependency files and imports."""
    write("requirements.txt", "corp-auth\nCorp_Billing\nflask-jwt-simple-auth\n")
    write("src/app.py", "import corp_auth\nimport corp_billing\nimport corp_tools_zz\n")
    registry = FakeRegistry(set())
    result = run(
        [tmp_path],
        tmp_path,
        registry,
        select=["SLOP001"],
        known_packages=["corp-auth", "corp_billing", "corp-tools-*"],
        check_imports=True,
    )
    # Only the genuinely unknown dependency is reported; known ones are never looked up.
    assert [(f.path, f.line) for f in result.findings] == [("requirements.txt", 3)]
    assert set(registry.queries) == {"flask-jwt-simple-auth"}


def test_known_packages_from_config_and_cli(tmp_path, monkeypatch, capsys):
    (tmp_path / "requirements.txt").write_text("corp-auth\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLOPFENCE_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "cache" / "slopfence").mkdir(parents=True)
    (tmp_path / "cache" / "slopfence" / "pypi.json").write_text(
        json.dumps({"corp-auth": [False, 9e12]})  # pinned "missing" far in the future
    )

    def rules():
        return [f["rule"] for f in json.loads(capsys.readouterr().out)["findings"]]

    main([".", "--format", "json"])
    assert rules() == ["SLOP001"]
    _pyproject(tmp_path, '[tool.slopfence]\nknown-packages = ["corp-*"]\n')
    assert main([".", "--format", "json"]) == 0
    assert rules() == []
    # --known-packages replaces the configured list.
    main([".", "--format", "json", "--known-packages", "other-lib"])
    assert rules() == ["SLOP001"]


# --- --fail-on (#18) --------------------------------------------------------


def test_fail_on_threshold(tmp_path, capsys):
    (tmp_path / "app.py").write_text(MEDIUM_ONLY)
    args = [str(tmp_path), "--offline", "--no-color"]
    assert main(args) == 1
    assert main([*args, "--fail-on", "medium"]) == 1
    assert main([*args, "--fail-on", "high"]) == 0
    # Findings are still reported when they don't fail the run.
    assert "SLOP051" in capsys.readouterr().out


def test_fail_on_from_config_and_cli_override(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text(MEDIUM_ONLY)
    _pyproject(tmp_path, '[tool.slopfence]\nfail-on = "high"\n')
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline"]) == 0
    assert main([".", "--offline", "--fail-on", "low"]) == 1


def test_high_finding_fails_every_threshold(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "from unittest.mock import Mock\n\ndef test_x():\n    m = Mock()\n    m.a = 1\n"
        "    assert m.a == 1\n"
    )
    for level in ("low", "medium", "high"):
        assert main([str(tmp_path), "--offline", "--fail-on", level]) == 1


# --- --strict (#19) ---------------------------------------------------------


def test_parse_errors_reported_but_not_fatal_by_default(tmp_path, capsys):
    (tmp_path / "broken.py").write_text(BROKEN)
    assert main([str(tmp_path), "--offline", "--no-color"]) == 0
    assert "warning: could not parse broken.py" in capsys.readouterr().out


def test_strict_makes_parse_errors_fatal(tmp_path, monkeypatch, capsys):
    (tmp_path / "broken.py").write_text(BROKEN)
    (tmp_path / "app.py").write_text(MEDIUM_ONLY)
    assert main([str(tmp_path), "--offline", "--no-color", "--strict"]) == 2
    assert "error: could not parse broken.py" in capsys.readouterr().out
    # --exit-zero still wins.
    assert main([str(tmp_path), "--offline", "--strict", "--exit-zero"]) == 0
    _pyproject(tmp_path, "[tool.slopfence]\nstrict = true\n")
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline"]) == 2
    assert main([".", "--offline", "--no-strict", "--fail-on", "high"]) == 0


def test_parse_errors_in_json_and_sarif(tmp_path, capsys):
    (tmp_path / "broken.py").write_text(BROKEN)
    main([str(tmp_path), "--offline", "--format", "json"])
    assert json.loads(capsys.readouterr().out)["parse_errors"] == ["broken.py"]

    for strict, level, ok in [([], "warning", True), (["--strict"], "error", False)]:
        main([str(tmp_path), "--offline", "--format", "sarif", *strict])
        [invocation] = json.loads(capsys.readouterr().out)["runs"][0]["invocations"]
        assert invocation["executionSuccessful"] is ok
        [note] = invocation["toolExecutionNotifications"]
        assert note["level"] == level
        assert note["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "broken.py"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[tool.slopfence]\nfail-on = "critical"\n', "'fail-on' must be one of high, medium, low"),
        ('[tool.slopfence]\nstrict = "yes"\n', "'strict' must be true or false"),
        ("[tool.slopfence]\ncheck-imports = 1\n", "'check-imports' must be true or false"),
        ('[tool.slopfence]\nknown-packages = "corp"\n', "'known-packages' must be a list"),
    ],
)
def test_invalid_new_config_values(tmp_path, body, message):
    _pyproject(tmp_path, body)
    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path)


def test_rule_lines_helper_still_works(write, check):
    write("src/app.py", MEDIUM_ONLY)
    assert rule_lines(check("SLOP051"), "SLOP051") == [1]
