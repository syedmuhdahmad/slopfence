---
name: preflight
description: Runs every check CI gates on (ruff, slopfence on its own code, tests with coverage, docstring coverage of touched functions, markdownlint) and reports pass or fail per check. Use before committing, before opening or updating a pull request, and whenever the question is "is this ready?" or "will CI pass?".
argument-hint: "[--quick]"
allowed-tools: Bash(python3 .claude/tools/preflight.py*)
---

# Preflight

Run the checks from the repository root:

```bash
python3 .claude/tools/preflight.py $ARGUMENTS
```

- With no arguments it runs everything CI gates on. It takes under a minute.
- `--quick` runs only ruff, slopfence on its own code, and the tests. Use it between edits.
- `--base <branch>` changes the branch the docstring check compares with (default `origin/main`).

## Reading the result

Each line is `ok`, `FAIL` or `skip`, and a failing check prints the end of its output.

| Check | When it fails |
|---|---|
| `ruff check` | Fix the code. Don't add `noqa` without a comment saying why. |
| `ruff format` | Run `.venv/bin/ruff format .` |
| `slopfence on its own code` | The finding is real: remove the duplicate, the placeholder or the silent `except`. Don't add an ignore comment to get past it. |
| `tests with coverage` | A failing test, or coverage under the minimum in `pyproject.toml`. Cover new branches with tests. |
| `docstrings on touched functions` | Add a one-line docstring to each function it lists. Test functions count. |
| `markdownlint` | Fix the Markdown it names. |

## Rules

- Fix the cause and run the script again until every line is `ok`. Never report success from a partial run.
- Show the final output when you report that the work is ready.
- A `skip` line means a tool is missing on this machine. Say so; CI will still run that check.
- CI also runs things this script can't: the test matrix (Python 3.10 to 3.14, macOS, Windows), the package build, and the end-to-end tests of the GitHub Action and the pre-commit hook. A clean preflight is necessary, not sufficient.
