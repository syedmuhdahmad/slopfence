import json

import pytest

from slopfence.cli import main
from slopfence.config import ConfigError, is_excluded, load_config
from tests.conftest import FakeRegistry, rule_lines
from tests.conftest import pyproject as _pyproject

SLOPPY = "# In a real implementation, retry\n# I hope this helps!\n"


def _rules(capsys):
    return sorted({f["rule"] for f in json.loads(capsys.readouterr().out)["findings"]})


def test_no_config_means_defaults(tmp_path):
    config = load_config(tmp_path)
    assert (config.select, config.ignore, config.exclude) == (None, [], [])
    _pyproject(tmp_path, "[tool.other]\nx = 1\n")
    assert load_config(tmp_path).select is None


def test_load_config_values(tmp_path):
    _pyproject(
        tmp_path,
        '[tool.slopfence]\nselect = ["slop010", "SLOP051"]\nignore = ["SLOP051"]\n'
        'exclude = ["migrations", "tests/fixtures/"]\n',
    )
    config = load_config(tmp_path)
    assert config.select == ["SLOP010", "SLOP051"]
    assert config.ignore == ["SLOP051"]
    assert config.exclude == ["migrations", "tests/fixtures/"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[tool.slopfence]\nmax-line-length = 100\n", "unknown key(s): max-line-length"),
        ('[tool.slopfence]\nselect = ["SLOP999"]\n', "unknown rule(s) in 'select': SLOP999"),
        ('[tool.slopfence]\nignore = "SLOP010"\n', "'ignore' must be a list of strings"),
        ("[tool.slopfence]\nselect = []\n", "'select' must not be empty"),
        ("[tool.slopfence\n", "invalid TOML"),
    ],
)
def test_invalid_config(tmp_path, body, message):
    _pyproject(tmp_path, body)
    with pytest.raises(ConfigError, match=message.replace("(", r"\(").replace(")", r"\)")):
        load_config(tmp_path)


def test_invalid_config_exits_2(tmp_path, monkeypatch, capsys):
    _pyproject(tmp_path, '[tool.slopfence]\nselect = ["SLOP999"]\n')
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline"]) == 2
    assert "SLOP999" in capsys.readouterr().err


def test_config_select_and_ignore_apply(tmp_path, monkeypatch, capsys):
    (tmp_path / "app.py").write_text(SLOPPY)
    monkeypatch.chdir(tmp_path)
    main([".", "--offline", "--format", "json"])
    assert _rules(capsys) == ["SLOP010", "SLOP051"]

    _pyproject(tmp_path, '[tool.slopfence]\nignore = ["SLOP051"]\n')
    main([".", "--offline", "--format", "json"])
    assert _rules(capsys) == ["SLOP010"]

    _pyproject(tmp_path, '[tool.slopfence]\nselect = ["SLOP051"]\n')
    main([".", "--offline", "--format", "json"])
    assert _rules(capsys) == ["SLOP051"]


def test_cli_overrides_config(tmp_path, monkeypatch, capsys):
    (tmp_path / "app.py").write_text(SLOPPY)
    _pyproject(tmp_path, '[tool.slopfence]\nselect = ["SLOP051"]\nignore = ["SLOP010"]\n')
    monkeypatch.chdir(tmp_path)
    main([".", "--offline", "--format", "json", "--select", "SLOP010"])
    assert _rules(capsys) == []  # CLI select replaces config select; config ignore still applies
    main([".", "--offline", "--format", "json", "--select", "SLOP010", "--ignore", "SLOP051"])
    assert _rules(capsys) == ["SLOP010"]


def test_exclude_from_config_and_cli(tmp_path, monkeypatch, capsys):
    for rel in ["app.py", "migrations/0001.py", "tests/fixtures/sloppy.py", "gen/x_pb2.py"]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SLOPPY)
    _pyproject(
        tmp_path, '[tool.slopfence]\nexclude = ["migrations", "tests/fixtures/", "*_pb2.py"]\n'
    )
    monkeypatch.chdir(tmp_path)

    def paths():
        return sorted({f["path"] for f in json.loads(capsys.readouterr().out)["findings"]})

    main([".", "--offline", "--format", "json"])
    assert paths() == ["app.py"]
    # Explicitly passed files are excluded too (pre-commit passes file names).
    main(["migrations/0001.py", "--offline", "--format", "json"])
    assert paths() == []
    # --exclude replaces the configured list.
    main([".", "--offline", "--format", "json", "--exclude", "app.py"])
    assert paths() == ["gen/x_pb2.py", "migrations/0001.py", "tests/fixtures/sloppy.py"]


@pytest.mark.parametrize(
    ("path", "patterns", "excluded"),
    [
        ("migrations/0001.py", ["migrations"], True),
        ("app/migrations/0001.py", ["migrations"], True),
        ("app/migrations/0001.py", ["migrations/"], True),
        ("tests/fixtures/a.py", ["tests/fixtures/"], True),
        ("src/tests/fixtures/a.py", ["tests/fixtures"], False),  # "/" anchors at the root
        ("gen/x_pb2.py", ["*_pb2.py"], True),
        ("src/gen/a.py", ["src/gen/*.py"], True),
        ("src/general.py", ["gen"], False),
        ("app.py", ["./app.py"], True),
        ("app.py", [""], False),
    ],
)
def test_is_excluded(path, patterns, excluded):
    assert is_excluded(path, patterns) is excluded


def test_excluded_modules_still_count_as_local(write, tmp_path):
    """Excluded files aren't checked, but importing them isn't a missing package."""
    from slopfence.engine import run

    write("src/app.py", "import generated_client_zz\n")
    write("src/generated_client_zz/__init__.py", "")
    result = run(
        [tmp_path],
        tmp_path,
        FakeRegistry(set()),
        select=["SLOP001"],
        exclude=["generated_*"],
        check_imports=True,
    )
    assert rule_lines(result, "SLOP001") == []
