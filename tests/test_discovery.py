"""File discovery: a nested checkout is another project, not this one's code (#58)."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from slopfence import engine as enginemod
from slopfence.engine import discover_all, run
from slopfence.registry import OfflineRegistry
from tests.conftest import FakeRegistry, rule_lines

DUPLICATE = '''
def format_date(value, fmt="%Y-%m-%d"):
    """Format a date for display."""
    if value is None:
        return ""
    text = value.strftime(fmt)
    return text.strip()
'''


def project(tmp_path: Path) -> Path:
    """A minimal project with one source file; returns its root."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "helpers.py").write_text(textwrap.dedent(DUPLICATE).lstrip("\n"))
    return tmp_path


def nested_checkout(tmp_path: Path, rel: str, git: str) -> Path:
    """A nested checkout at ``rel`` holding a copy of the project's file.

    ``git`` is ``"file"`` for a worktree or submodule (a ``.git`` file) and ``"dir"``
    for a clone (a ``.git`` directory).
    """
    root = tmp_path / rel
    (root / "src").mkdir(parents=True)
    (root / "src" / "helpers.py").write_text(textwrap.dedent(DUPLICATE).lstrip("\n"))
    marker = root / ".git"
    if git == "file":
        marker.write_text("gitdir: ../../../.git/worktrees/demo\n")
    else:
        marker.mkdir()
    return root


def test_nested_worktree_git_file_is_not_scanned(tmp_path):
    """A worktree (a .git file) inside the project is not this project's code."""
    project(tmp_path)
    nested_checkout(tmp_path, ".claude/worktrees/demo", "file")
    found, _, _ = discover_all([tmp_path], tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in found] == ["src/helpers.py"]
    # No false SLOP030: the copy in the worktree is not compared against src/helpers.py.
    assert run([tmp_path], tmp_path, OfflineRegistry(), select=["SLOP030"]).findings == []


def test_nested_clone_git_directory_is_not_scanned(tmp_path):
    """A clone (a .git directory) inside the project is not this project's code."""
    project(tmp_path)
    nested_checkout(tmp_path, "vendor/library", "dir")
    found, _, _ = discover_all([tmp_path], tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in found] == ["src/helpers.py"]
    assert run([tmp_path], tmp_path, OfflineRegistry(), select=["SLOP030"]).findings == []


def test_explicitly_passed_checkout_is_still_scanned(tmp_path):
    """A checkout named on the command line is checked, even though it holds a .git."""
    checkout = nested_checkout(tmp_path, ".claude/worktrees/demo", "file")
    copy = textwrap.dedent(DUPLICATE).lstrip("\n").replace("format_date", "formatDate", 1)
    (checkout / "src" / "views.py").write_text(copy)
    result = run([checkout], tmp_path, OfflineRegistry(), select=["SLOP030"])
    assert [(f.path, f.line) for f in result.findings] == [
        (".claude/worktrees/demo/src/views.py", 1)
    ]


def test_module_in_nested_checkout_is_not_a_local_module(tmp_path):
    """A module that only exists in a nested checkout is not this project's own."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("import thing\n")
    checkout = nested_checkout(tmp_path, ".claude/worktrees/demo", "file")
    (checkout / "src" / "thing.py").write_text("x = 1\n")
    result = run([tmp_path], tmp_path, FakeRegistry(set()), select=["SLOP001"], check_imports=True)
    assert rule_lines(result, "SLOP001") == [1]


def test_dependency_files_in_nested_checkout_are_not_the_projects(tmp_path):
    """A nested checkout's private index and package name say nothing about this project.

    If they did, the index would stop the lookup of ``ghost-lib`` and the name would
    make ``other-copy`` one of the project's own packages.
    """
    (tmp_path / "setup.cfg").write_text(
        "[options]\ninstall_requires =\n    ghost-lib\n    other-copy\n"
    )
    checkout = nested_checkout(tmp_path, "vendor/library", "dir")
    (checkout / "requirements.txt").write_text("--index-url https://pkgs.corp.example/simple\n")
    (checkout / "pyproject.toml").write_text('[project]\nname = "other-copy"\n')
    registry = FakeRegistry(set())
    result = run([tmp_path], tmp_path, registry, select=["SLOP001"])
    assert [(f.path, f.subject) for f in result.findings] == [
        ("setup.cfg", "ghost-lib"),
        ("setup.cfg", "other-copy"),
    ]
    assert result.notes == []
    assert sorted(set(registry.queries)) == ["ghost-lib", "other-copy"]


# --- Links and special files: a project can hold more than plain files. ---

CHAT = "# Sure, here is the token: s3cr3t for you\nx = 1\n"

needs_symlinks = pytest.mark.skipif(
    os.name == "nt", reason="making a symbolic link needs a privilege on Windows"
)


class RecordingRegistry(FakeRegistry):
    """A registry where nothing exists, which records every name it is asked about."""

    def __init__(self) -> None:
        """Start with no package and no question."""
        super().__init__(set())


def outside(tmp_path: Path) -> tuple[Path, Path]:
    """A project folder and, next to it, a folder that is not part of it."""
    root, other = tmp_path / "proj", tmp_path / "other"
    root.mkdir()
    other.mkdir()
    return root, other


@needs_symlinks
def test_link_to_a_python_file_outside_the_project_is_not_read(tmp_path):
    """A link in the project to a file elsewhere is not the project's code: nothing
    of that file is read, so nothing of it can show up in a report."""
    root, other = outside(tmp_path)
    (other / "private.py").write_text(CHAT, encoding="utf-8")
    (root / "innocent.py").symlink_to(other / "private.py")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    result = run([root], root, OfflineRegistry())
    assert result.findings == []
    assert result.files_checked == 1
    assert discover_all([root], root)[0] == [root / "app.py"]


@needs_symlinks
def test_link_to_a_dependency_file_outside_the_project_sends_nothing(tmp_path):
    """The lines of a file outside the project are not package names of the project:
    PyPI is not asked about them."""
    root, other = outside(tmp_path)
    (other / "secret.txt").write_text("internal-corp-token-abc123\n", encoding="utf-8")
    (root / "requirements.txt").symlink_to(other / "secret.txt")
    registry = RecordingRegistry()
    result = run([root], root, registry)
    assert registry.queries == []
    assert result.findings == []


@needs_symlinks
def test_link_that_is_named_on_the_command_line_is_not_followed_out_of_the_project(tmp_path):
    """pre-commit names the files of a commit one by one. A link among them is no
    more the project's code than in a walk."""
    root, other = outside(tmp_path)
    (other / "private.py").write_text(CHAT, encoding="utf-8")
    (root / "innocent.py").symlink_to(other / "private.py")
    result = run([root / "innocent.py"], root, OfflineRegistry())
    assert result.findings == []
    assert result.files_checked == 0


@needs_symlinks
def test_link_inside_the_project_is_still_read(tmp_path):
    """A link to another file of the same project is read as before."""
    root, _ = outside(tmp_path)
    (root / "real").mkdir()
    (root / "real" / "app.py").write_text(CHAT, encoding="utf-8")
    (root / "link.py").symlink_to(root / "real" / "app.py")
    assert discover_all([root], root)[0] == [root / "link.py", root / "real" / "app.py"]
    result = run([root], root, OfflineRegistry(), select=["SLOP051"])
    # A file is reported under its real path, so the two readings give one finding.
    assert [f.path for f in result.findings] == ["real/app.py"]
    assert result.files_checked == 2


@needs_symlinks
def test_file_outside_the_project_that_is_named_itself_is_still_checked(tmp_path):
    """Naming a file is a choice. Also when it is a link, as long as the link is not
    one of the project's own files."""
    root, other = outside(tmp_path)
    (other / "private.py").write_text(CHAT, encoding="utf-8")
    (other / "link.py").symlink_to(other / "private.py")
    for named in ("private.py", "link.py"):
        result = run([other / named], root, OfflineRegistry(), select=["SLOP051"])
        assert rule_lines(result, "SLOP051") == [1]


@needs_symlinks
def test_link_out_of_a_named_folder_outside_the_project_is_not_read(tmp_path):
    """A folder outside the project that is named is walked like a project of its
    own: a link out of it is not read."""
    root, other = outside(tmp_path)
    (tmp_path / "third").mkdir()
    (tmp_path / "third" / "private.py").write_text(CHAT, encoding="utf-8")
    (other / "innocent.py").symlink_to(tmp_path / "third" / "private.py")
    (other / "app.py").write_text(CHAT, encoding="utf-8")
    result = run([other], root, OfflineRegistry(), select=["SLOP051"])
    assert len(result.findings) == 1 and result.findings[0].path.endswith("app.py")


@needs_symlinks
def test_link_to_an_endless_device_does_not_hang_the_run(tmp_path):
    """``app.py -> /dev/zero`` never ends when it is read. It is not read."""
    root, _ = outside(tmp_path)
    (root / "app.py").symlink_to("/dev/zero")
    (root / "requirements.txt").symlink_to("/dev/zero")
    assert run([root], root, OfflineRegistry()).files_checked == 0


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this system has no named pipes")
def test_named_pipe_is_not_read(tmp_path):
    """A pipe named like a source file blocks whoever opens it. An unpacked archive
    can hold one. It is passed over."""
    root, _ = outside(tmp_path)
    os.mkfifo(root / "app.py")
    os.mkfifo(root / "requirements.txt")
    (root / "real.py").write_text("x = 1\n", encoding="utf-8")
    assert discover_all([root], root) == ([root / "real.py"], [], [])
    assert run([root], root, OfflineRegistry()).files_checked == 1


@needs_symlinks
def test_link_whose_target_is_gone_is_handled_as_before(tmp_path):
    """A dangling link inside the project is still listed: reading it says what is wrong."""
    root, _ = outside(tmp_path)
    (root / "gone.py").symlink_to(root / "missing.py")
    assert discover_all([root], root)[0] == [root / "gone.py"]
    # Reported under the path the link names, as every linked file is.
    assert run([root], root, OfflineRegistry()).parse_errors == ["missing.py"]


@needs_symlinks
def test_link_that_leads_back_to_itself_is_passed_over(tmp_path):
    """``loop.py -> loop.py`` names no file. Before Python 3.13 it ended the run with
    an error when its path was resolved."""
    root, _ = outside(tmp_path)
    (root / "loop.py").symlink_to(root / "loop.py")
    (root / "a.py").symlink_to(root / "b.py")
    (root / "b.py").symlink_to(root / "a.py")
    (root / "real.py").write_text("x = 1\n", encoding="utf-8")
    assert discover_all([root], root)[0] == [root / "real.py"]
    result = run([root], root, OfflineRegistry())
    assert result.files_checked == 1 and result.parse_errors == []


@needs_symlinks
def test_named_link_out_of_the_project_lends_no_dependency_files(tmp_path, monkeypatch):
    """A named path brings the dependency files of its own folder along. A link that
    is not followed brings none: the folder it leads to is not searched."""
    root, other = outside(tmp_path)
    (other / "private.py").write_text("x = 1\n", encoding="utf-8")
    (other / "requirements.txt").write_text("internal-corp-token-abc123\n", encoding="utf-8")
    (root / "innocent.py").symlink_to(other / "private.py")
    read = []
    real = enginemod.build_index

    def recording(root, modules, dependency_files, lock_files, **options):
        """Note which dependency and lock files the run reads."""
        read.extend([*dependency_files, *lock_files])
        return real(root, modules, dependency_files, lock_files, **options)

    monkeypatch.setattr(enginemod, "build_index", recording)
    registry = RecordingRegistry()
    result = run([root / "innocent.py"], root, registry)
    assert read == []
    assert registry.queries == []
    assert result.files_checked == 0


@needs_symlinks
def test_named_link_to_a_folder_outside_the_project_is_not_walked(tmp_path):
    """A link in the project to a folder elsewhere, named on the command line, is not
    a way into that folder: none of its files is read."""
    root, other = outside(tmp_path)
    (other / "private.py").write_text(CHAT, encoding="utf-8")
    (other / "requirements.txt").write_text("internal-corp-token-abc123\n", encoding="utf-8")
    (root / "vendor").symlink_to(other, target_is_directory=True)
    assert discover_all([root / "vendor"], root) == ([], [], [])
    registry = RecordingRegistry()
    result = run([root / "vendor"], root, registry)
    assert result.findings == []
    assert result.files_checked == 0
    assert registry.queries == []


@needs_symlinks
def test_named_folder_is_still_walked_through_a_link_inside_the_project(tmp_path):
    """A link to a folder of the same project is followed, and a folder outside the
    project that is named itself is still checked."""
    root, other = outside(tmp_path)
    (root / "pkg").mkdir()
    (root / "pkg" / "app.py").write_text(CHAT, encoding="utf-8")
    (root / "alias").symlink_to(root / "pkg", target_is_directory=True)
    (other / "app.py").write_text(CHAT, encoding="utf-8")
    assert discover_all([root / "alias"], root)[0] == [root / "pkg" / "app.py"]
    assert discover_all([other], root)[0] == [other / "app.py"]


@needs_symlinks
def test_named_link_that_leads_back_to_itself_is_passed_over(tmp_path):
    """A link in a loop names nothing, inside the project or outside it. Named itself,
    it ended a run on Python 3.10 to 3.12 with an error when its path was resolved."""
    root, other = outside(tmp_path)
    (root / "loop.py").symlink_to(root / "loop.py")
    (other / "a.py").symlink_to(other / "b.py")
    (other / "b.py").symlink_to(other / "a.py")
    (root / "real.py").write_text("x = 1\n", encoding="utf-8")
    for named in (root / "loop.py", other / "a.py"):
        assert discover_all([named], root) == ([], [], [])
        result = run([named, root / "real.py"], root, OfflineRegistry())
        assert result.files_checked == 1
        assert result.parse_errors == []
