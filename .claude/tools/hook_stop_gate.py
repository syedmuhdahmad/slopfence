#!/usr/bin/env python3
"""Stop hook: Claude can't end a turn while changed Python code fails the quick checks.

"Looks done" is not a signal. When ``src/`` or ``tests/`` has uncommitted Python
changes, this runs ``preflight.py --quick`` (ruff, slopfence on itself, the tests)
and, if anything fails, hands the output back so the work continues.

It stays out of the way: it does nothing when no Python changed, when the same
changes already passed, or when it has already sent Claude back once this turn.
Set ``SLOPFENCE_SKIP_STOP_GATE=1`` to turn it off for a session.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from devenv import emit, git, read_event, repo_root, same_repository

PREFLIGHT = Path(__file__).with_name("preflight.py")
# The paths whose changes are worth testing.
CODE_DIRS = ("src", "tests")
TIMEOUT_SECONDS = 240


def changed_python(root: Path) -> list[str]:
    """Python files under the code folders with uncommitted changes, untracked ones included."""
    status = git(root, "status", "--porcelain", "--untracked-files=all", "--", *CODE_DIRS) or ""
    files = [line[3:].split(" -> ")[-1].strip('"') for line in status.splitlines()]
    return sorted(f for f in files if f.endswith(".py"))


def fingerprint(root: Path, files: list[str]) -> str:
    """A hash of the changed files' content, so identical changes are checked only once."""
    digest = hashlib.sha256()
    for name in files:
        digest.update(name.encode())
        try:
            digest.update((root / name).read_bytes())
        except OSError:
            digest.update(b"<deleted>")
    return digest.hexdigest()


def passed_marker(root: Path) -> Path:
    """Where the fingerprint of the last changes that passed is remembered, per checkout."""
    key = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"slopfence-stop-gate-{key}"


def already_passed(marker: Path, state: str) -> bool:
    """Whether exactly these changes passed the checks before."""
    try:
        return marker.read_text(encoding="utf-8") == state
    except OSError:
        return False  # no marker yet: these changes have not been checked


def run_quick_checks(root: Path) -> subprocess.CompletedProcess[str]:
    """Run ``preflight.py --quick`` on ``root``; raises ``TimeoutExpired`` if it hangs."""
    return subprocess.run(
        [sys.executable, str(PREFLIGHT), "--quick", "--root", str(root)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def main() -> int:
    """Block the stop with the failing output when the quick checks fail."""
    event = read_event()
    if event.get("stop_hook_active") or os.environ.get("SLOPFENCE_SKIP_STOP_GATE"):
        return 0
    root = repo_root(Path(event.get("cwd") or "."))
    if root is None or not same_repository(root, event):
        return 0
    files = changed_python(root)
    if not files:
        return 0
    state, marker = fingerprint(root, files), passed_marker(root)
    if already_passed(marker, state):
        return 0

    try:
        done = run_quick_checks(root)
    except subprocess.TimeoutExpired:
        # A gate that can't finish must not trap the session, but the user should know.
        emit(
            {
                "systemMessage": "slopfence stop gate: the quick checks did not finish in "
                f"{TIMEOUT_SECONDS}s, so the changes were not checked."
            }
        )
        return 0
    except OSError:
        return 0  # a gate that can't run must not trap the session
    if done.returncode == 0:
        # Without the marker the checks simply run again next time.
        with contextlib.suppress(OSError):
            marker.write_text(state, encoding="utf-8")
        return 0
    if done.returncode != 1:
        return 0  # preflight could not run (no virtualenv): nothing to report
    emit(
        {
            "decision": "block",
            "reason": "The quick checks fail with the current changes to src/ or tests/ "
            "(python3 .claude/tools/preflight.py --quick):\n\n" + done.stdout.strip(),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
