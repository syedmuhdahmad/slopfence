import json

import pytest

from slopfence.cli import main
from slopfence.config import ConfigError, find_root, is_excluded, load_config
from tests.conftest import FakeRegistry, git_init, rule_lines
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


SILENT = (
    "def load():\n    try:\n        return open('x').read()\n    except Exception:\n        pass\n"
)


def _report(capsys, *argv):
    """Run slopfence offline with JSON output; return (exit code, [(path, rule)])."""
    code = main(["--offline", "--format", "json", *argv])
    findings = json.loads(capsys.readouterr().out)["findings"]
    return code, [(f["path"], f["rule"]) for f in findings]


@pytest.fixture
def no_git_project(tmp_path):
    """The project of #69, in no git repository: a config that ignores SLOP040, and
    code with a SLOP040 and a SLOP051 finding."""
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    _pyproject(proj, '[tool.slopfence]\nignore = ["SLOP040"]\n')
    (proj / "src" / "app.py").write_text(SILENT + "# I hope this helps!\n", encoding="utf-8")
    return proj


@pytest.mark.parametrize(
    ("start", "path"),
    [("proj", "src"), ("proj/src", "."), (".", "proj/src"), ("proj", "src/app.py")],
)
def test_config_is_found_from_any_start_folder_without_git(
    no_git_project, monkeypatch, capsys, start, path
):
    """Without git, the config applies wherever slopfence is started, and the paths
    in the report are the same (#69)."""
    monkeypatch.chdir(no_git_project.parent / start)
    assert _report(capsys, path) == (1, [("src/app.py", "SLOP051")])


def test_baseline_paths_do_not_depend_on_the_start_folder_without_git(
    no_git_project, monkeypatch, capsys
):
    """A baseline written from one folder hides the same findings from another (#69)."""
    baseline = no_git_project / "accepted.json"
    monkeypatch.chdir(no_git_project)
    assert main(["--offline", "--write-baseline", str(baseline), "."]) == 0
    monkeypatch.chdir(no_git_project / "src")
    capsys.readouterr()
    assert _report(capsys, "--baseline", str(baseline), ".") == (0, [])


def test_find_root_takes_the_nearest_folder_that_configures_slopfence(tmp_path):
    """A pyproject.toml without a [tool.slopfence] table does not hide the one above it
    that has the table: a package of a larger project uses the project's config."""
    package = tmp_path / "packages" / "one"
    (package / "src").mkdir(parents=True)
    _pyproject(tmp_path, "[tool.slopfence]\nstrict = true\n")
    _pyproject(package, "[tool.other]\nx = 1\n")
    assert find_root(package / "src") == tmp_path
    _pyproject(package, "[tool.slopfence]\nstrict = false\n")
    assert find_root(package / "src") == package
    assert find_root(package) == package


def test_find_root_takes_the_nearest_pyproject_when_none_configures_slopfence(tmp_path):
    """Without a [tool.slopfence] table anywhere, the nearest pyproject.toml marks the project."""
    package = tmp_path / "packages" / "one"
    (package / "src").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'all'\n", encoding="utf-8")
    assert find_root(package / "src") == tmp_path
    (package / "pyproject.toml").write_text("[project]\nname = 'one'\n", encoding="utf-8")
    assert find_root(package / "src") == package


def test_find_root_stops_at_a_pyproject_that_can_not_be_read(tmp_path):
    """A broken pyproject.toml may configure slopfence. It is the root, so the run
    reports it, as it does when slopfence is started in that folder."""
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text("[tool.slopfence\n", encoding="utf-8")
    assert find_root(tmp_path / "src") == tmp_path
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(find_root(tmp_path / "src"))


def test_a_folder_named_pyproject_toml_is_no_config(tmp_path):
    """Only a file counts."""
    (tmp_path / "src" / "pyproject.toml").mkdir(parents=True)
    _pyproject(tmp_path, "[tool.slopfence]\nstrict = true\n")
    assert find_root(tmp_path / "src") == tmp_path


@pytest.mark.parametrize("tool", ["tool = 1\n", "[tool]\nslopfence = 1\n"])
def test_tool_that_is_no_table_is_handled(tmp_path, tool):
    """``tool = 1`` is valid TOML: it is no config, and a table that is none is an error."""
    (tmp_path / "pyproject.toml").write_text(tool, encoding="utf-8")
    if "slopfence" in tool:
        with pytest.raises(ConfigError, match="must be a table"):
            load_config(tmp_path)
    else:
        assert load_config(tmp_path).select is None
    assert find_root(tmp_path) == tmp_path


def test_broken_config_above_the_start_folder_is_an_error_without_git(
    tmp_path, monkeypatch, capsys
):
    """Started in a sub-folder, a config that is not valid is reported, not passed over (#69)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    _pyproject(tmp_path, '[tool.slopfence]\nignore = ["SLOP999"]\n')
    monkeypatch.chdir(tmp_path / "src")
    assert main(["--offline", "."]) == 2
    assert "unknown rule(s) in 'ignore': SLOP999" in capsys.readouterr().err


def test_inside_a_git_repository_the_root_is_still_the_repository(tmp_path, monkeypatch, capsys):
    """git decides first, as before: a pyproject.toml below the top of the repository
    does not become the root (#69)."""
    git_init(tmp_path)
    package = tmp_path / "packages" / "one"
    package.mkdir(parents=True)
    _pyproject(package, '[tool.slopfence]\nignore = ["SLOP051"]\n')
    (package / "app.py").write_text("# I hope this helps!\n", encoding="utf-8")
    monkeypatch.chdir(package)
    assert _report(capsys, ".") == (1, [("packages/one/app.py", "SLOP051")])


def test_without_git_and_without_pyproject_the_root_is_the_start_folder(
    tmp_path, monkeypatch, capsys
):
    """Nothing marks a project: the current folder is the root, as before (#69)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("# I hope this helps!\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert _report(capsys, "src") == (1, [("src/app.py", "SLOP051")])
    monkeypatch.chdir(tmp_path / "src")
    assert _report(capsys, ".") == (1, [("app.py", "SLOP051")])
