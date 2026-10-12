---
paths:
  - "tests/**/*.py"
---

# Test rules

- Tests never touch the network or the machine's pip and uv settings. Use `FakeRegistry`, and leave the autouse `no_index_config` fixture alone.
- Build projects with the `write` fixture and scan them with `check` (both in `tests/conftest.py`). Read results with `rule_lines(result, "SLOP0xx")`. For a config file or a git repository in a test, use `pyproject()`, `git()` and `git_init()` from the same file instead of writing them again.
- For a detector, write the cases that SHOULD be flagged and the look-alike cases that should NOT. Name each test after the behaviour: `test_narrow_exception_is_not_flagged`.
- Assert on real behaviour. slopfence's own rules run on this folder: a test that only asserts on its own mocks, asserts `True`, or hides failures in `try/except` fails CI.
- One short docstring per test saying what it proves. CodeRabbit counts test functions in its docstring check.
- Tests must pass on Windows and macOS: no hard-coded `/` paths in assertions about real files, no reliance on file order, and `encoding="utf-8"` when a file has non-ASCII text.
- While iterating, run one file: `.venv/bin/python -m pytest tests/test_cli.py -q`. Run the whole suite before you finish.
- Coverage (lines and branches) must stay at or above 90%. Cover a new branch with a test, not with `# pragma: no cover`.
