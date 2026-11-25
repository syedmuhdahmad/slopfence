"""How slopfence finds and starts git (#92): by a full path from PATH, never from the project."""

import os
import subprocess
from pathlib import Path

import pytest

from slopfence import diff as diffmod
from slopfence.cli import main
from tests.conftest import git, git_init

# What the git program is called in a folder of PATH.
GIT_NAME = "git.exe" if os.name == "nt" else "git"


def _program(folder: Path) -> Path:
    """Put a file that looks like a git program in ``folder`` and return its path."""
    folder.mkdir(parents=True, exist_ok=True)
    program = folder / GIT_NAME
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(0o755)
    return program


def _started(monkeypatch, environments: list | None = None) -> list[list[str]]:
    """Record the command of every program that ``diff`` starts from now on, and in
    ``environments``, when given, the environment each one gets."""
    commands: list[list[str]] = []
    real = subprocess.run

    def record(command, **kwargs):
        """Note the command and its environment, then run it."""
        commands.append(list(command))
        if environments is not None:
            environments.append(kwargs.get("env"))
        return real(command, **kwargs)

    monkeypatch.setattr(diffmod.subprocess, "run", record)
    return commands


def test_git_is_found_in_the_folders_of_the_search_path(tmp_path):
    """The first folder of PATH that holds a git program wins, as in a shell."""
    first, second = _program(tmp_path / "one"), _program(tmp_path / "two")
    (tmp_path / "empty").mkdir()
    folders = [tmp_path / "empty", first.parent, second.parent]
    assert diffmod.find_git(os.pathsep.join(map(str, folders))) == str(first)


def test_git_is_not_found_where_there_is_none(tmp_path):
    """A PATH without git gives None, also when a folder of it does not exist."""
    (tmp_path / "empty").mkdir()
    search_path = os.pathsep.join([str(tmp_path / "empty"), str(tmp_path / "missing")])
    assert diffmod.find_git(search_path) is None
    assert diffmod.find_git("") is None


def test_a_folder_named_git_is_not_the_program(tmp_path):
    """Only a file can be started."""
    (tmp_path / "bin" / GIT_NAME).mkdir(parents=True)
    assert diffmod.find_git(str(tmp_path / "bin")) is None


@pytest.mark.skipif(os.name == "nt", reason="every file can be started on Windows")
def test_a_file_that_can_not_be_started_is_passed_over(tmp_path):
    """A file named git without the permission to run it is not git."""
    _program(tmp_path / "one").chmod(0o644)
    real = _program(tmp_path / "two")
    search_path = os.pathsep.join([str(tmp_path / "one"), str(real.parent)])
    assert diffmod.find_git(search_path) == str(real)


@pytest.mark.parametrize("entry", ["", ".", "bin", os.path.join(".", "bin")])
def test_git_is_never_taken_from_the_current_folder(tmp_path, monkeypatch, entry):
    """An entry of PATH that is no full path names the current folder or a folder in
    it, and that is the project being checked. A git there is not slopfence's to start."""
    project = tmp_path / "project"
    _program(project)
    _program(project / "bin")
    real = _program(tmp_path / "real")
    monkeypatch.chdir(project)
    assert diffmod.find_git(entry) is None
    assert diffmod.find_git(os.pathsep.join([entry, str(real.parent)])) == str(real)


def test_quotes_around_a_folder_of_the_search_path_are_dropped(tmp_path):
    """Windows allows ``"C:\\Program Files\\Git\\cmd"`` in PATH, with the quotes."""
    program = _program(tmp_path / "with space")
    assert diffmod.find_git(f'"{program.parent}"') == str(program)


def test_git_is_started_by_its_full_path(tmp_path, monkeypatch):
    """Every git command names the program in full. Started by its bare name, Windows
    would look for it in the current folder first: the project."""
    git_init(tmp_path)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "init")
    _program(tmp_path)  # a "git" in the project itself
    monkeypatch.chdir(tmp_path)
    commands = _started(monkeypatch)
    assert diffmod.repo_root(tmp_path) == tmp_path.resolve()
    assert diffmod.changed_lines(tmp_path, "HEAD") == {GIT_NAME: None}
    assert len(commands) == 4
    for command in commands:
        program = Path(command[0])
        assert program.is_absolute() and program.name.lower() == GIT_NAME
        assert program.parent.resolve() != tmp_path.resolve()


@pytest.mark.skipif(os.name == "nt", reason="needs a program that a shell script can stand in for")
def test_a_git_in_the_project_is_not_run_when_the_search_path_names_the_current_folder(
    tmp_path, monkeypatch, capsys
):
    """With ``.`` or an empty entry in PATH, a program named git in the checked folder
    would be the one that runs. It must not be."""
    git_init(tmp_path)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "init")
    planted = tmp_path / "git"
    planted.write_text('#!/bin/sh\necho ran > "$(dirname "$0")/ran.txt"\n', encoding="utf-8")
    planted.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", os.environ["PATH"]]))
    assert main([".", "--offline", "--diff", "HEAD", "--select", "SLOP051"]) == 0
    assert not (tmp_path / "ran.txt").exists()
    assert "No issues found" in capsys.readouterr().out


def test_without_git_on_the_search_path_diff_is_an_error(tmp_path, monkeypatch, capsys):
    """ "git is not installed" is still what --diff says, and the exit code is still 2."""
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert diffmod.repo_root(tmp_path) is None
    assert main([".", "--offline", "--diff", "HEAD"]) == 2
    assert "--diff HEAD: git is not installed" in capsys.readouterr().err


def test_without_git_a_plain_run_still_works(tmp_path, monkeypatch):
    """Only --diff needs git: a run without it treats the folder as no repository."""
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")
    assert main([".", "--offline"]) == 0


def test_git_that_goes_away_before_it_is_started_is_not_installed(tmp_path, monkeypatch):
    """A program that was found and can't be started any more is reported the same way."""
    monkeypatch.setattr(diffmod, "find_git", lambda search_path: str(tmp_path / "gone" / "git"))
    with pytest.raises(diffmod.DiffError, match="git is not installed"):
        diffmod.changed_lines(tmp_path, "HEAD")


@pytest.mark.parametrize("ref", ["--output=stolen.txt", "-h", "--all"])
def test_a_ref_that_looks_like_an_option_is_turned_down(tmp_path, monkeypatch, capsys, ref):
    """``--diff=--output=...`` must not reach git as an option of its own: no git
    command is started for it, and the run ends with an error."""
    git_init(tmp_path)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)
    assert diffmod.repo_root(tmp_path) == tmp_path.resolve()
    commands = _started(monkeypatch)
    with pytest.raises(diffmod.DiffError, match="can't start with '-'"):
        diffmod.changed_lines(tmp_path, ref)
    assert commands == []
    assert main([".", "--offline", f"--diff={ref}"]) == 2
    assert "can't start with '-'" in capsys.readouterr().err
    assert not (tmp_path / "stolen.txt").exists()


def test_diff_does_not_run_the_program_the_repository_names_as_file_monitor(tmp_path):
    """``core.fsmonitor`` in a repository's own config can name a program, and git runs
    it for a diff. slopfence switches that off: asking what changed runs nothing."""
    git_init(tmp_path)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "init")
    (tmp_path / "app.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    marker = tmp_path.parent / f"{tmp_path.name}-ran.txt"
    git(tmp_path, "config", "core.fsmonitor", f"echo ran > '{marker.as_posix()}'; echo")
    assert diffmod.changed_lines(tmp_path, "HEAD") == {"app.py": {2}}
    assert not marker.exists()


def test_every_git_command_is_read_only_and_ignores_the_file_monitor(tmp_path, monkeypatch):
    """The options that keep git from running a program of the repository, or from
    writing to it, are on every command. The file monitor is switched off with an empty
    value: a git before 2.36 takes ``false`` for the name of a program to run."""
    git_init(tmp_path)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "init")
    environments: list[dict[str, str]] = []
    commands = _started(monkeypatch, environments)
    diffmod.repo_root(tmp_path)
    diffmod.changed_lines(tmp_path, "HEAD")
    assert len(commands) == 4
    for command, environment in zip(commands, environments, strict=True):
        assert command[1:3] == ["-c", "core.fsmonitor="]
        assert environment["GIT_OPTIONAL_LOCKS"] == "0"
        assert environment["PATH"] == os.environ["PATH"], "the rest of the environment stays"


@pytest.mark.skipif(os.name != "nt", reason="a path without a drive is only a Windows matter")
def test_folder_without_a_drive_is_no_full_path_on_windows(tmp_path):
    """``\\tools`` names a folder on the current drive, which is the project's drive."""
    _program(tmp_path / "tools")
    assert diffmod.find_git("\\tools") is None
