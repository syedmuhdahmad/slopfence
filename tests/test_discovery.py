"""File discovery: a nested checkout is another project, not this one's code (#58)."""

from __future__ import annotations

import textwrap
from pathlib import Path

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
    found, _, _ = discover_all([tmp_path])
    assert [p.relative_to(tmp_path).as_posix() for p in found] == ["src/helpers.py"]
    # No false SLOP030: the copy in the worktree is not compared against src/helpers.py.
    assert run([tmp_path], tmp_path, OfflineRegistry(), select=["SLOP030"]).findings == []


def test_nested_clone_git_directory_is_not_scanned(tmp_path):
    """A clone (a .git directory) inside the project is not this project's code."""
    project(tmp_path)
    nested_checkout(tmp_path, "vendor/library", "dir")
    found, _, _ = discover_all([tmp_path])
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
