# slopfence

A fast, deterministic linter for the junk AI coding assistants leave behind. The package is in `src/slopfence`, the tests in `tests`.

## Priorities, in this order

1. **Low false positives over high recall.** Flag only obvious cases. A noisy rule gets the tool uninstalled.
2. **Deterministic.** Same input, same output, same order.
3. **Standard library only.** No runtime dependencies (`tomli` on Python 3.10 is the one exception).
4. **Python 3.10 to 3.14 on Linux, macOS and Windows.** Your local Python is probably newer than 3.10, so check that every API you use exists in 3.10.
5. **Network: `pypi.org` only.** A registry that can't be reached never produces a finding.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'   # setup
.venv/bin/python -m pytest tests/test_cli.py -q             # one test file: prefer this while iterating
.venv/bin/python -m pytest -q                               # whole suite
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/slopfence src tests                               # slopfence must be clean on its own code
python3 .claude/tools/preflight.py                          # everything CI checks, in one run
```

## Workflow

- IMPORTANT: never commit on `main`. Create a branch first: `feat/...`, `fix/...`, `docs/...` or `chore/...`. Everything reaches `main` through a squash-merged pull request.
- New detectors, features and behaviour changes need an agreed issue first. Link it with `Closes #N`.
- A behaviour change needs tests, a `CHANGELOG.md` entry under `[Unreleased]`, and README updates when options, rules or output change.
- Pull request titles follow Conventional Commits (`feat: ...`, `fix: ...`). The title becomes the squash commit.
- CodeRabbit gates every pull request: docstrings on at least 80% of the functions you touch (tests included), and every acceptance criterion of the linked issue met.
- Before you say work is done, run the preflight script and show its output.

## Gotchas

- Tests never touch the network. Use `FakeRegistry` and the `write` / `check` fixtures in `tests/conftest.py`.
- `.github/action-fixture/` is sloppy on purpose, because CI scans it. Don't fix it.
- Index URLs can hold credentials. Never print or log them.
- slopfence's own rules run on this repo in CI: a duplicated function, a silent `except Exception: pass` or a placeholder comment fails the build.
- CI fails if the sdist grows past 300 KB. It ships `src/`, `tests/`, `pyproject.toml`, `README.md`, `CHANGELOG.md` and `LICENSE`, and nothing from `docs/` or `.github/`.

## Compaction

When compacting, keep the branch name, the linked issue number, the list of modified files, and the commands that verify the work.
