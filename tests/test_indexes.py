"""Private package indexes configured outside dependency files (#17)."""

import json
import os
import sys

import pytest

from slopfence.cli import main
from slopfence.config import ConfigError, load_config
from slopfence.indexes import find_private_index, is_public_index
from slopfence.registry import PyPIRegistry
from tests.conftest import FakeRegistry, rule_lines

PRIVATE = "https://user:secret@pkgs.corp.example/simple"


@pytest.mark.parametrize(
    ("url", "public"),
    [
        ("https://pypi.org/simple", True),
        ("https://PyPI.org/simple/", True),
        ("https://files.pythonhosted.org/packages", True),
        ("https://pypi.python.org/simple", True),
        # TestPyPI is another index with other packages, and so is its file host.
        ("https://test.pypi.org/simple/", False),
        ("https://test-files.pythonhosted.org/packages", False),
        ("https://pkgs.corp.example/simple", False),
        ("https://corp.example/pypi.org/simple", False),
        ("https://pypi.org.corp.example/simple", False),
        ("not a url", False),
        (None, False),
    ],
)
def test_is_public_index(url, public):
    """Only PyPI's own hosts are public, matched by the whole host and never by a part of it."""
    assert is_public_index(url) is public


def isolated_env(tmp_path, **extra):
    """An environment whose pip/uv config locations are all inside tmp_path."""
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "XDG_CONFIG_DIRS": str(tmp_path / "xdg-system"),
        "APPDATA": str(tmp_path / "appdata"),
        "PROGRAMDATA": str(tmp_path / "programdata"),
    }
    env.update(extra)
    return env


def no_config(tmp_path, **extra):
    """Like isolated_env, but with pip's and uv's config files switched off."""
    return isolated_env(tmp_path, PIP_CONFIG_FILE=os.devnull, UV_NO_CONFIG="1", **extra)


@pytest.mark.parametrize(
    "var",
    ["PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX_URL"],
)
def test_private_index_in_environment(tmp_path, var):
    """A non-PyPI index in pip's or uv's environment variables is detected."""
    env = no_config(tmp_path, **{var: PRIVATE})
    found = find_private_index(tmp_path, env, tmp_path)
    assert found == f"the {var} environment variable"
    assert "secret" not in found  # never reveal the URL, it can hold credentials


def test_uv_index_variable_with_names(tmp_path):
    """UV_INDEX holds space-separated "name=url" entries."""
    env = no_config(tmp_path, UV_INDEX=f"pypi=https://pypi.org/simple corp={PRIVATE}")
    assert find_private_index(tmp_path, env, tmp_path) == "the UV_INDEX environment variable"
    env = no_config(tmp_path, UV_INDEX="pypi=https://pypi.org/simple")
    assert find_private_index(tmp_path, env, tmp_path) is None


def test_public_indexes_in_environment_are_fine(tmp_path):
    """Pointing pip or uv at PyPI explicitly is not a private index."""
    env = no_config(
        tmp_path,
        PIP_INDEX_URL="https://pypi.org/simple",
        PIP_EXTRA_INDEX_URL="https://pypi.org/simple https://files.pythonhosted.org/x",
    )
    assert find_private_index(tmp_path, env, tmp_path) is None


def _user_pip_conf(tmp_path):
    """Where pip reads the user's config file on this platform, inside tmp_path."""
    if sys.platform == "win32":
        return tmp_path / "appdata" / "pip" / "pip.ini"
    return tmp_path / "xdg" / "pip" / "pip.conf"


@pytest.mark.parametrize(
    "body",
    [
        f"[global]\nindex-url = {PRIVATE}\n",
        f"[install]\nextra-index-url =\n    https://pypi.org/simple\n    {PRIVATE}\n",
    ],
)
def test_private_index_in_user_pip_config(tmp_path, body):
    """pip.conf / pip.ini in the user's config folder is read ([global] and [install])."""
    conf = _user_pip_conf(tmp_path)
    conf.parent.mkdir(parents=True)
    conf.write_text(body)
    env = isolated_env(tmp_path, UV_NO_CONFIG="1")
    found = find_private_index(tmp_path, env, tmp_path)
    assert found == f"pip configuration ({conf})"


@pytest.mark.parametrize("var", ["XDG_DATA_DIRS", "XDG_DATA_HOME"])
def test_macos_pip_config_in_xdg_data_dirs(tmp_path, monkeypatch, var):
    """pip 26.2+ on macOS reads config from XDG_DATA_DIRS and XDG_DATA_HOME."""
    from slopfence import indexes

    monkeypatch.setattr(indexes, "PLATFORM", "darwin")
    conf = tmp_path / "data" / "pip" / "pip.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(f"[global]\nindex-url = {PRIVATE}\n")
    env = isolated_env(tmp_path, UV_NO_CONFIG="1", **{var: str(tmp_path / "data")})
    assert find_private_index(tmp_path, env, tmp_path) == f"pip configuration ({conf})"
    # Elsewhere pip doesn't read these folders, so neither does slopfence.
    monkeypatch.setattr(indexes, "PLATFORM", "linux")
    assert find_private_index(tmp_path, env, tmp_path) is None


def test_pip_config_file_variable(tmp_path):
    """PIP_CONFIG_FILE adds a config file, and os.devnull switches config files off."""
    conf = tmp_path / "custom.conf"
    conf.write_text(f"[global]\nindex-url = {PRIVATE}\n")
    env = isolated_env(tmp_path, UV_NO_CONFIG="1", PIP_CONFIG_FILE=str(conf))
    assert find_private_index(tmp_path, env, tmp_path) == f"pip configuration ({conf})"

    user_conf = _user_pip_conf(tmp_path)
    user_conf.parent.mkdir(parents=True)
    user_conf.write_text(f"[global]\nindex-url = {PRIVATE}\n")
    assert find_private_index(tmp_path, no_config(tmp_path), tmp_path) is None


def test_virtualenv_pip_config(tmp_path):
    """A pip.conf inside the active virtualenv is read."""
    venv = tmp_path / "venv"
    venv.mkdir()
    name = "pip.ini" if sys.platform == "win32" else "pip.conf"
    (venv / name).write_text(f"[global]\nextra-index-url = {PRIVATE}\n")
    env = isolated_env(tmp_path, UV_NO_CONFIG="1", VIRTUAL_ENV=str(venv))
    assert find_private_index(tmp_path, env, tmp_path) == f"pip configuration ({venv / name})"


@pytest.mark.parametrize(
    "body",
    [
        f'[[index]]\nname = "corp"\nurl = "{PRIVATE}"\n',
        f'extra-index-url = ["{PRIVATE}"]\n',
    ],
)
def test_private_index_in_project_uv_toml(tmp_path, body):
    """A project's uv.toml is read like [tool.uv] in pyproject.toml."""
    (tmp_path / "uv.toml").write_text(body)
    env = isolated_env(tmp_path, PIP_CONFIG_FILE=os.devnull)
    found = find_private_index(tmp_path, env, tmp_path)
    assert found == f"uv configuration ({tmp_path / 'uv.toml'})"


@pytest.mark.parametrize(
    "body",
    [f'[pip]\nindex-url = "{PRIVATE}"\n', f'[pip]\nextra-index-url = ["{PRIVATE}"]\n'],
)
def test_private_index_in_uv_pip_settings(tmp_path, body):
    """uv's [pip] table, used by ``uv pip install``, counts too."""
    (tmp_path / "uv.toml").write_text(body)
    env = isolated_env(tmp_path, PIP_CONFIG_FILE=os.devnull)
    found = find_private_index(tmp_path, env, tmp_path)
    assert found == f"uv configuration ({tmp_path / 'uv.toml'})"


def test_private_index_in_tool_uv_pip(write, check):
    """[tool.uv.pip] in pyproject.toml makes that file's dependencies private."""
    write(
        "pyproject.toml",
        f'[project]\nname = "demo"\ndependencies = ["corp-auth"]\n\n'
        f'[tool.uv.pip]\nindex-url = "{PRIVATE}"\n',
    )
    registry = FakeRegistry(set())
    assert check("SLOP001", registry=registry).findings == []
    assert registry.queries == []


def test_system_config_files(tmp_path, monkeypatch):
    """System-wide pip.conf and uv.toml (e.g. /etc/pip.conf) are read; tests use stand-ins."""
    from slopfence import indexes

    pip_conf = tmp_path / "etc-pip.conf"
    pip_conf.write_text(f"[global]\nindex-url = {PRIVATE}\n")
    monkeypatch.setattr(indexes, "PIP_SYSTEM_FILES", (pip_conf,))
    env = isolated_env(tmp_path, UV_NO_CONFIG="1")
    if sys.platform != "win32":  # Windows has no fixed system paths, only PROGRAMDATA
        assert find_private_index(tmp_path, env, tmp_path) == f"pip configuration ({pip_conf})"

    monkeypatch.setattr(indexes, "PIP_SYSTEM_FILES", ())
    uv_toml = tmp_path / "etc-uv.toml"
    uv_toml.write_text(f'index-url = "{PRIVATE}"\n')
    monkeypatch.setattr(indexes, "UV_SYSTEM_FILES", (uv_toml,))
    env = isolated_env(tmp_path, PIP_CONFIG_FILE=os.devnull)
    if sys.platform != "win32":
        assert find_private_index(tmp_path, env, tmp_path) == f"uv configuration ({uv_toml})"


def test_uv_explicit_index_and_no_config(tmp_path):
    """An explicit uv index only serves pinned packages; UV_NO_CONFIG skips uv.toml."""
    (tmp_path / "uv.toml").write_text(
        f'[[index]]\nname = "corp"\nurl = "{PRIVATE}"\nexplicit = true\n'
    )
    env = isolated_env(tmp_path, PIP_CONFIG_FILE=os.devnull)
    assert find_private_index(tmp_path, env, tmp_path) is None
    (tmp_path / "uv.toml").write_text(f'index-url = "{PRIVATE}"\n')
    assert find_private_index(tmp_path, no_config(tmp_path), tmp_path) is None


def test_broken_config_files_are_ignored(tmp_path):
    """Unreadable pip or uv config never crashes the run."""
    conf = tmp_path / "broken.conf"
    conf.write_text("[global\nindex-url")
    (tmp_path / "uv.toml").write_text("[[index\n")
    env = isolated_env(tmp_path, PIP_CONFIG_FILE=str(conf))
    assert find_private_index(tmp_path, env, tmp_path) is None


# --- Effect on SLOP001 -------------------------------------------------------------


def test_private_environment_index_skips_all_lookups(write, check):
    """With a private index configured, nothing is looked up and a note explains why."""
    write("requirements.txt", "corp-auth\n")
    write("app.py", "import corp_billing\n")
    registry = FakeRegistry(set())
    result = check(
        "SLOP001",
        registry=registry,
        check_imports=True,
        private_index="the PIP_INDEX_URL environment variable",
    )
    assert result.findings == []
    assert registry.queries == []
    assert len(result.notes) == 1 and "PIP_INDEX_URL" in result.notes[0]


def test_private_index_in_a_project_file_skips_import_lookups(write, check):
    """If any project file uses a private index, unknown imports may be private too."""
    write("requirements.txt", "--extra-index-url https://pkgs.corp.example/simple\ncorp-lib\n")
    write("app.py", "import corp_billing\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, check_imports=True)
    assert result.findings == [] and registry.queries == []
    assert result.notes and "requirements.txt" in result.notes[0]


def test_private_pipfile_source_skips_import_lookups(write, check):
    """A Pipfile [[source]] that isn't PyPI counts as a private index."""
    write("Pipfile", f'[[source]]\nname = "corp"\nurl = "{PRIVATE}"\n\n[packages]\n')
    write("app.py", "import corp_billing\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, check_imports=True)
    assert result.findings == [] and registry.queries == []
    assert "Pipfile" in result.notes[0]


def test_pdm_source_in_pyproject(write, check):
    """[[tool.pdm.source]] that isn't PyPI makes that file's dependencies private."""
    write(
        "pyproject.toml",
        f'[project]\nname = "demo"\ndependencies = ["corp-auth"]\n\n'
        f'[[tool.pdm.source]]\nname = "corp"\nurl = "{PRIVATE}"\n',
    )
    registry = FakeRegistry(set())
    assert check("SLOP001", registry=registry).findings == []
    assert registry.queries == []


@pytest.fixture
def cli_project(tmp_path, monkeypatch):
    """A project with an unknown dependency and PyPI answers pinned in the cache."""
    (tmp_path / "requirements.txt").write_text("corp-auth\n")
    cache = tmp_path / "cache" / "slopfence"
    cache.mkdir(parents=True)
    (cache / "pypi.json").write_text(json.dumps({"corp-auth": [False, 9e12]}))
    monkeypatch.setenv("SLOPFENCE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)

    def no_network(self, key):
        """Fail the test if a lookup isn't answered from the pinned cache."""
        raise AssertionError(f"unexpected network lookup for {key!r}")

    monkeypatch.setattr(PyPIRegistry, "_query", no_network)
    return tmp_path


def test_cli_private_index_note_and_override(cli_project, monkeypatch, capsys):
    """The CLI detects PIP_INDEX_URL; --no-detect-private-index and config turn it off."""
    monkeypatch.setenv("PIP_INDEX_URL", PRIVATE)
    assert main([".", "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["findings"] == []
    assert "PIP_INDEX_URL" in out["notes"][0] and "secret" not in out["notes"][0]

    assert main([".", "--no-color"]) == 0
    assert "note: SLOP001 and SLOP002 made no PyPI lookups" in capsys.readouterr().out

    assert main([".", "--format", "json", "--no-detect-private-index"]) == 1
    assert [f["rule"] for f in json.loads(capsys.readouterr().out)["findings"]] == ["SLOP001"]

    (cli_project / "pyproject.toml").write_text("[tool.slopfence]\ndetect-private-index = false\n")
    assert main([".", "--format", "sarif"]) == 1
    run = json.loads(capsys.readouterr().out)["runs"][0]
    assert [r["ruleId"] for r in run["results"]] == ["SLOP001"]
    assert run["invocations"][0]["toolExecutionNotifications"] == []


def test_cli_sarif_note(cli_project, monkeypatch, capsys):
    """The note also appears in SARIF as a tool notification."""
    monkeypatch.setenv("PIP_INDEX_URL", PRIVATE)
    main([".", "--format", "sarif"])
    notes = json.loads(capsys.readouterr().out)["runs"][0]["invocations"][0][
        "toolExecutionNotifications"
    ]
    assert [n["level"] for n in notes] == ["note"]


def test_detect_private_index_config_type(tmp_path):
    """detect-private-index must be a boolean."""
    (tmp_path / "pyproject.toml").write_text('[tool.slopfence]\ndetect-private-index = "no"\n')
    with pytest.raises(ConfigError, match="'detect-private-index' must be true or false"):
        load_config(tmp_path)


def test_rule_lines_unaffected_without_private_index(write, check):
    """Without any private index, an unknown dependency is still reported."""
    write("requirements.txt", "ghost-dep\n")
    assert rule_lines(check("SLOP001", registry=FakeRegistry(set())), "SLOP001") == [1]
