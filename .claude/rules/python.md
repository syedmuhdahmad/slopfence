---
paths:
  - "src/**/*.py"
  - "tests/**/*.py"
  - ".claude/**/*.py"
---

# Python coding standards

## Design

- **One job per module and per function.** The pipeline is: find files (`engine`), parse (`source`), per-file detectors (`detectors/`), project-wide rules (duplicates, packages), filters (ignore comments, `--diff`), then `reporters`. Put new code in the stage it belongs to. A stage never reaches into another stage's internals.
- **Extend, don't edit.** A new rule is one entry in `models.RULES` and one function registered in `detectors.FILE_DETECTORS`. If `engine.run` needs a new `if` for your rule, the design is wrong.
- **Depend on protocols, not classes.** Code takes a `Registry`, never a `PyPIRegistry`. Every implementation (`FakeRegistry`, `OfflineRegistry`) must keep working unchanged when you change the protocol.
- **Small interfaces.** Pass the few values a function needs, not the whole config or the `argparse` namespace.
- **Detectors are pure.** `SourceFile` in, `Finding`s out. No file or network access, no global state, no printing.
- **No speculative abstractions.** Add a base class, option or parameter when a second real use exists, not before.

## Reuse before you write

- Look for an existing helper first: `source.relative_path`, `source.read_source`, `registry.normalize`, `config.is_excluded`, `diff.touches`, and `SourceFile.is_test_file` / `is_example_file` / `text_spans`.
- When two places need the same logic, move it into one function and import it. Never copy a function, even under a different name: `SLOP030` runs on this repo and fails CI.

## Style

- `from __future__ import annotations` at the top of every module, and type hints on every signature.
- Every function, method and class has a docstring that says what it does and anything surprising about it. One line is fine.
- Comments say why, never what the next line does.
- Value objects are frozen dataclasses. No mutable module-level state.
- Catch the narrowest exception that can happen. If you ignore an error on purpose, say why in a comment.
- Read and write text with `encoding="utf-8"`, build paths with `pathlib`, and report paths as project-relative POSIX strings.
- Lines are at most 100 characters. Ruff rules `E, F, W, I, B, UP, SIM, RUF` must pass. A `noqa` needs a comment that says why.
