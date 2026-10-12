"""Per-file ignores and per-rule severity overrides (#52)."""

import pytest

from slopfence.cli import main
from slopfence.config import ConfigError, ignored_rules, load_config
from slopfence.models import Severity


def pyproject(tmp_path, body):
    """Write a pyproject.toml whose slopfence part is ``body``."""
    text = "[project]\nname = 'demo'\n\n" + body
    (tmp_path / "pyproject.toml").write_text(text, encoding="utf-8")


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
