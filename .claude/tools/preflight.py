#!/usr/bin/env python3
"""Run the checks CI runs, locally, and print one line per check.

Usage:
    python3 .claude/tools/preflight.py            # everything CI gates on
    python3 .claude/tools/preflight.py --quick    # lint, self-check and tests only

Exit code 0 means every check passed (skipped checks don't fail the run).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from devenv import repo_root, venv_python

TOOLS = Path(__file__).resolve().parent
# Lines of a failing command's output to show: enough to see the error, not a whole log.
TAIL_LINES = 40
# Longest one command may run. The slowest (tests with coverage) takes well under a minute.
COMMAND_TIMEOUT = 300.0


@dataclass(frozen=True)
class Check:
    """A named check: commands that must all exit 0, run in order."""

    name: str
    commands: tuple[tuple[str, ...], ...]
    # Why the check can't run here (a missing tool); None when it can.
    skip: str | None = None


def build_checks(root: Path, python: str, quick: bool, base: str) -> list[Check]:
    """The checks to run, in the order CI reports them."""
    checks = [
        Check("ruff check", ((python, "-m", "ruff", "check", "."),)),
        Check("ruff format", ((python, "-m", "ruff", "format", "--check", "."),)),
        Check("slopfence on its own code", ((python, "-m", "slopfence", "src", "tests"),)),
    ]
    if quick:
        pytest = (python, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider")
        return [*checks, Check("tests", (pytest,))]
    coverage = (python, "-m", "coverage")
    checks.append(
        Check(
            "tests with coverage (minimum in pyproject.toml)",
            (
                (*coverage, "erase"),
                (*coverage, "run", "-m", "pytest", "-q"),
                (*coverage, "combine"),
                (*coverage, "report", "--skip-covered"),
            ),
        )
    )
    checks.append(
        Check(
            "docstrings on touched functions (CodeRabbit wants 80%)",
            ((python, str(TOOLS / "docstring_coverage.py"), "--base", base, "--min", "80"),),
        )
    )
    if (TOOLS / "test_tools.py").is_file():
        tool_tests = (python, "-m", "pytest", str(TOOLS), "-q", "-p", "no:cacheprovider")
        checks.append(Check("tests of the Claude Code tool scripts", (tool_tests,)))
    npx = shutil.which("npx")
    checks.append(
        Check(
            "markdownlint",
            ((npx or "npx", "--yes", "markdownlint-cli2", "**/*.md", "!.venv/**"),),
            skip=None if npx else "npx is not installed",
        )
    )
    return checks


def run_check(check: Check, root: Path, timeout: float = COMMAND_TIMEOUT) -> tuple[bool, str]:
    """Run a check's commands in ``root``; return whether it passed and the failing output.

    A command that runs longer than ``timeout`` seconds fails the check.
    """
    for command in check.commands:
        try:
            done = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, f"{' '.join(command[:4])} ... did not finish in {timeout:.0f}s"
        except OSError as err:
            return False, f"could not run {command[0]}: {err}"
        if done.returncode != 0:
            lines = (done.stdout + done.stderr).strip().splitlines()
            return False, "\n".join(lines[-TAIL_LINES:])
    return True, ""


def main(argv: list[str] | None = None) -> int:
    """Run the checks and return 0 if all passed, 1 if any failed, 2 if they can't run."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true", help="lint, self-check and tests only")
    parser.add_argument("--root", type=Path, help="checkout to check (default: the current one)")
    parser.add_argument("--base", default="origin/main", help="branch the work will merge into")
    args = parser.parse_args(argv)

    root = repo_root(args.root or Path.cwd())
    if root is None:
        print("preflight: not inside a git repository", file=sys.stderr)
        return 2
    python = venv_python(root)
    if python is None:
        print(
            "preflight: no .venv here. Create it with:\n"
            "  python -m venv .venv && .venv/bin/pip install -e '.[dev]'",
            file=sys.stderr,
        )
        return 2

    failed = 0
    for check in build_checks(root, python, args.quick, args.base):
        if check.skip:
            print(f"skip - {check.name} ({check.skip})")
            continue
        passed, output = run_check(check, root)
        print(f"{'ok  ' if passed else 'FAIL'} - {check.name}")
        if not passed:
            failed += 1
            print("\n".join(f"    {line}" for line in output.splitlines()))
    print(f"\n{failed} check(s) failed" if failed else "\nAll checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
