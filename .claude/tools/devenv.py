"""Helpers shared by this project's Claude Code hooks and tool scripts.

Everything here must work with any Python 3.10+ and the standard library only:
hooks run with the system ``python3``, not the project's virtualenv.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Branches nobody commits or pushes to directly; changes arrive through pull requests.
PROTECTED_BRANCHES = frozenset({"main", "master"})


def read_event() -> dict:
    """The hook event Claude Code sends on stdin, or an empty dict if there is none."""
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return {}
    return event if isinstance(event, dict) else {}


def emit(payload: dict) -> None:
    """Send a JSON result back to Claude Code on stdout."""
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")


def pre_tool_decision(decision: str, reason: str) -> None:
    """Answer a PreToolUse event with ``allow``, ``deny`` or ``ask`` and the reason."""
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        }
    )


def git(cwd: Path, *args: str) -> str | None:
    """Output of a git command run in ``cwd``, or None if git fails or is missing."""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def existing_dir(path: Path) -> Path:
    """``path`` if it is a directory, otherwise its nearest existing parent directory."""
    for candidate in (path, *path.parents):
        if candidate.is_dir():
            return candidate
    return Path.cwd()


def repo_root(start: Path) -> Path | None:
    """Top folder of the git checkout (or worktree) that contains ``start``."""
    out = git(existing_dir(start), "rev-parse", "--show-toplevel")
    return Path(out) if out else None


def current_branch(cwd: Path) -> str | None:
    """Branch checked out in ``cwd``; None for a detached HEAD or outside a repository."""
    return git(existing_dir(cwd), "symbolic-ref", "--short", "-q", "HEAD") or None


def git_common_dir(start: Path) -> Path | None:
    """The shared ``.git`` folder of a checkout; the same for every worktree of one repository."""
    where = existing_dir(start)
    out = git(where, "rev-parse", "--git-common-dir")
    return (where / out).resolve() if out else None


def project_dir(event: dict) -> Path:
    """Where the Claude Code session started: this project's main checkout."""
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".")


def same_repository(path: Path, event: dict) -> bool:
    """Whether ``path`` is inside this project's repository, in any of its worktrees.

    Hooks must leave other repositories alone, such as throwaway test repositories
    created under a temporary folder.
    """
    ours = git_common_dir(project_dir(event))
    return ours is not None and git_common_dir(path) == ours


def venv_python(root: Path) -> str | None:
    """The interpreter of the virtualenv in ``root``, if there is one."""
    for candidate in (
        root / ".venv" / "bin" / "python",
        root / ".venv" / "Scripts" / "python.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return None
