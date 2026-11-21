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
    writing to it, are on every command."""
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
        assert command[1:3] == ["-c", "core.fsmonitor=false"]
        assert environment["GIT_OPTIONAL_LOCKS"] == "0"
        assert environment["PATH"] == os.environ["PATH"], "the rest of the environment stays"


# --- What a diff says changed: read from git's output, whatever the files hold. ---

BODY = """\
    total = 0
    for item in items:
        if item.price > limit:
            total += item.price * rate
        else:
            total += item.price
    total = round(total, 2)
    return total
"""


def _committed(root: Path, files: dict[str, bytes]) -> None:
    """Make ``root`` a repository with one commit that holds ``files`` (path -> bytes)."""
    git_init(root)
    # The bytes of the files are what the tests are about: git must not convert them.
    git(root, "config", "core.autocrlf", "false")
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")


@pytest.mark.parametrize("planted", ["++ /dev/null", "++ b/other.py", "++ b/app.py\t"])
def test_an_added_line_that_looks_like_a_file_header_is_only_a_line(tmp_path, planted):
    """An added line ``++ /dev/null`` reads ``+++ /dev/null`` in a diff, like the
    header of a deleted file. It must not end the file: the changes below it would be
    lost, and a finding there would pass a --diff run."""
    old = b"x = 1\ny = 2\nz = 3\n\n\ndef f():\n    return 1\n"
    _committed(tmp_path, {"app.py": old, "other.py": b"a = 1\n"})
    new = f'x = 1\nDOC = """\n{planted}\n"""\ny = 2\nz = 3\n\n\ndef f():\n    # new\n    return 1\n'
    (tmp_path / "app.py").write_bytes(new.encode())
    assert diffmod.changed_lines(tmp_path, "HEAD") == {"app.py": {2, 3, 4, 10}}


def test_a_finding_below_a_planted_header_line_is_still_reported(tmp_path, monkeypatch, capsys):
    """The same, from the command line: the placeholder below the planted line is found."""
    _committed(tmp_path, {"app.py": b"x = 1\ny = 2\n\n\ndef f():\n    return 1\n"})
    (tmp_path / "app.py").write_bytes(
        b'x = 1\nDOC = """\n++ /dev/null\n"""\ny = 2\n\n\ndef f():\n'
        b"    # In a real implementation, use the cache\n    return 1\n"
    )
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline", "--diff", "HEAD"]) == 1
    assert "SLOP010" in capsys.readouterr().out


@pytest.mark.parametrize("separator", ["\r", "\x0c", "\x1c", "\x85", "\u2028"])
def test_only_a_line_feed_ends_a_line_of_a_diff(tmp_path, separator):
    """A carriage return, a form feed and the like are part of a line. Read as line
    ends, they would put the rest of the line at the start of a line of its own,
    where ``+++ /dev/null`` passes for a header."""
    _committed(tmp_path, {"app.py": b"x = 1\ny = 2\nz = 3\n\n\n\nw = 4\n"})
    new = f"x = 1\nplanted = 0  #{separator}+++ /dev/null\ny = 2\nz = 3\n\n\n\nw = 4\nv = 5\n"
    (tmp_path / "app.py").write_bytes(new.encode())
    assert diffmod.changed_lines(tmp_path, "HEAD") == {"app.py": {2, 9}}


def test_files_with_windows_line_ends_have_the_same_changed_lines(tmp_path):
    """``\\r\\n`` at the end of every line changes nothing about which lines changed."""
    _committed(tmp_path, {"app.py": b"x = 1\r\ny = 2\r\n"})
    (tmp_path / "app.py").write_bytes(b"x = 1\r\nnew = 0\r\ny = 2\r\nlast = 3\r\n")
    assert diffmod.changed_lines(tmp_path, "HEAD") == {"app.py": {2, 4}}


def test_a_file_without_a_final_line_end_is_read_to_its_last_line(tmp_path):
    """git adds a note after the last line of such a file. It is no line of the file."""
    _committed(tmp_path, {"app.py": b"x = 1\ny = 2", "next.py": b"a = 1\n"})
    (tmp_path / "app.py").write_bytes(b"x = 1\ny = 3")
    (tmp_path / "next.py").write_bytes(b"a = 1\nb = 2\n")
    assert diffmod.changed_lines(tmp_path, "HEAD") == {"app.py": {2}, "next.py": {2}}


@pytest.mark.parametrize("name", ["my file.py", "a b/c d.py"])
def test_a_file_with_a_space_in_its_name_has_its_changed_lines(tmp_path, name):
    """git ends the header of such a file with a tab. The tab is not part of the name."""
    _committed(tmp_path, {name: b"x = 1\n"})
    (tmp_path / name).write_bytes(b"x = 1\ny = 2\n")
    assert diffmod.changed_lines(tmp_path, "HEAD") == {name: {2}}


def test_a_finding_in_a_file_with_a_space_in_its_name_is_reported(tmp_path, monkeypatch, capsys):
    """From the command line: --diff no longer passes over such a file."""
    _committed(tmp_path, {"my file.py": b"x = 1\n"})
    (tmp_path / "my file.py").write_bytes(b"x = 1\n# In a real implementation, do it\n")
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline", "--diff", "HEAD"]) == 1
    assert "my file.py" in capsys.readouterr().out


def test_removed_lines_are_marked_where_they_were(tmp_path):
    """Lines that were only taken out leave no changed line. The place is kept: the
    number of the line that now comes before it, 0 at the top of the file."""
    _committed(tmp_path, {"app.py": b"a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n"})
    (tmp_path / "app.py").write_bytes(b"b = 2\nc = 3\ne = 5\n")
    changed = diffmod.changed_lines(tmp_path, "HEAD")
    assert changed == {"app.py": {diffmod.Removed(0), diffmod.Removed(2)}}


def test_lines_replaced_by_fewer_lines_are_changed_lines(tmp_path):
    """Three lines that become one: that line changed, and nothing else is marked."""
    _committed(tmp_path, {"app.py": b"a = 1\nb = 2\nc = 3\nd = 4\n"})
    (tmp_path / "app.py").write_bytes(b"a = 1\nx = 0\nd = 4\n")
    assert diffmod.changed_lines(tmp_path, "HEAD") == {"app.py": {2}}


def test_touches_counts_lines_removed_inside_a_span():
    """A function is changed when lines were taken out between two of its lines. A
    finding on one line is not: no line of it changed."""
    changed = {"app.py": {diffmod.Removed(4)}}
    assert diffmod.touches(changed, "app.py", 3, 9)
    assert diffmod.touches(changed, "app.py", 4, 5)
    assert not diffmod.touches(changed, "app.py", 4, None)
    assert not diffmod.touches(changed, "app.py", 4, 4)
    assert not diffmod.touches(changed, "app.py", 5, 9), "removed above its first line"
    assert not diffmod.touches(changed, "app.py", 1, 4), "removed below its last line"
    assert not diffmod.touches(changed, "other.py", 3, 9)


def _two_functions(root: Path) -> None:
    """The project of #71: ``sum_prices`` is ``total_price`` plus one line."""
    a = f"def total_price(items, limit, rate):\n{BODY}"
    b = f"def sum_prices(items, limit, rate):\n    print(items)\n{BODY}"
    b += "\n\ndef other(x):\n    y = x + 1\n    print(y)\n    z = y * 2\n    return z\n"
    _committed(root, {"a.py": a.encode(), "b.py": b.encode()})


def test_function_that_becomes_a_copy_by_losing_a_line_is_reported_with_diff(
    tmp_path, monkeypatch, capsys
):
    """Deleting the one line that made two functions differ adds no line, and still
    makes a copy: --diff reports it, as the next full run would (#71)."""
    _two_functions(tmp_path)
    # Bytes in and bytes out: written as text, every line would get a Windows line
    # end there, and git would see the whole file as changed.
    b = (tmp_path / "b.py").read_bytes()
    (tmp_path / "b.py").write_bytes(b.replace(b"    print(items)\n", b""))
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline", "--diff", "HEAD"]) == 1
    out = capsys.readouterr().out
    assert "SLOP030" in out and "'sum_prices' is identical to 'total_price' in a.py:1" in out
    assert main([".", "--offline"]) == 1
    assert "'sum_prices' is identical to 'total_price' in a.py:1" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("    print(y)\n", ""),  # a line of a function that is no copy of anything
        ("\n\ndef other", "\ndef other"),  # a blank line between two functions
    ],
)
def test_deletion_that_makes_no_copy_reports_nothing_with_diff(
    tmp_path, monkeypatch, capsys, old, new
):
    """Taking a line out somewhere else changes nothing: no finding appears (#71)."""
    _two_functions(tmp_path)
    b = (tmp_path / "b.py").read_bytes()
    assert old.encode() in b
    (tmp_path / "b.py").write_bytes(b.replace(old.encode(), new.encode()))
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline", "--diff", "HEAD"]) == 0
    assert "No issues found" in capsys.readouterr().out


def test_deletion_next_to_an_old_finding_does_not_report_it(tmp_path, monkeypatch, capsys):
    """A finding on one line is not new because the line after it was deleted."""
    _committed(tmp_path, {"app.py": b"x = 1\n# In a real implementation, do it\ny = 2\nz = 3\n"})
    (tmp_path / "app.py").write_bytes(b"x = 1\n# In a real implementation, do it\nz = 3\n")
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline", "--diff", "HEAD"]) == 0
    assert "No issues found" in capsys.readouterr().out


def test_parse_unified_diff_reads_a_hunk_by_its_counts():
    """The header of a hunk says how many lines follow. Those lines are never read as
    headers, whatever they look like."""
    text = (
        "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
        "@@ -2,2 +2,3 @@\n--- a/gone.py\n-x\n+++ /dev/null\n+@@ -1 +1 @@\n+diff --git a/x b/x\n"
        "@@ -9,2 +9,0 @@\n-a\n-b\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -0,0 +1 @@\n+new\n"
    )
    assert diffmod.parse_unified_diff(text) == {
        "app.py": {2, 3, 4, diffmod.Removed(9)},
        "b.py": {1},
    }


@pytest.mark.skipif(os.name != "nt", reason="a path without a drive is only a Windows matter")
def test_folder_without_a_drive_is_no_full_path_on_windows(tmp_path):
    """``\\tools`` names a folder on the current drive, which is the project's drive."""
    _program(tmp_path / "tools")
    assert diffmod.find_git("\\tools") is None
