#!/usr/bin/env python3
"""SessionStart hook: tell Claude which branch it is on and whether the project is set up.

Claude Code adds what this prints to the session as context. It states facts only.
"""

from __future__ import annotations

import sys
from pathlib import Path

from devenv import PROTECTED_BRANCHES, current_branch, read_event, repo_root, venv_python


def session_facts(root: Path) -> list[str]:
    """Short facts about the checkout that change how work should start."""
    facts = []
    branch = current_branch(root)
    if branch in PROTECTED_BRANCHES:
        facts.append(
            f"The checkout is on '{branch}', which is protected. Code changes in this "
            "repository are made on a new branch (feat/..., fix/..., docs/..., chore/...)."
        )
    elif branch:
        facts.append(f"The checkout is on branch '{branch}'.")
    if venv_python(root) is None:
        facts.append(
            "There is no .venv in this checkout, so tests and ruff can't run yet. Setup: "
            "python -m venv .venv && .venv/bin/pip install -e '.[dev]'"
        )
    return facts


def main() -> int:
    """Print the facts for the checkout the session started in."""
    root = repo_root(Path(read_event().get("cwd") or "."))
    if root is not None:
        print("\n".join(session_facts(root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
