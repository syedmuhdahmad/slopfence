#!/usr/bin/env python3
"""PostToolUse hook for Edit and Write: format the Python file that was just changed.

CI fails on ``ruff format --check``, so formatting happens here every time instead
of relying on someone remembering it. The hook never blocks: if ruff is missing or
fails, the edit simply stays as written.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from devenv import project_dir, read_event, repo_root, same_repository, venv_python


def edited_file(event: dict) -> Path | None:
    """The file the tool wrote, taken from the tool's input or its result."""
    for key, field in (("tool_input", "file_path"), ("tool_response", "filePath")):
        section = event.get(key)
        value = section.get(field) if isinstance(section, dict) else None
        if isinstance(value, str) and value:
            return Path(value)
    return None


def format_file(path: Path, event: dict) -> None:
    """Run ``ruff format`` on ``path`` with the project's own ruff, if it has one."""
    root = repo_root(path.parent)
    if root is None or not same_repository(root, event):
        return
    # A worktree without its own virtualenv uses the main checkout's ruff.
    python = venv_python(root) or venv_python(project_dir(event))
    if python is None:
        return
    try:
        # --force-exclude keeps ruff's excludes (the sloppy CI fixture) for explicit paths.
        subprocess.run(
            [python, "-m", "ruff", "format", "--force-exclude", "--quiet", str(path)],
            cwd=root,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def main() -> int:
    """Format the edited file when it is a Python file in this repository."""
    event = read_event()
    path = edited_file(event)
    if path is not None and path.suffix == ".py" and path.is_file():
        format_file(path, event)
    return 0


if __name__ == "__main__":
    sys.exit(main())
