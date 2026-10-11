# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Upgrade notes

- On projects with 200 or more Python files, slopfence now starts worker processes: one for every 100 files, up to one per CPU and at most 8. Findings are the same. To switch this off, pass `--jobs 1` or set `jobs = 1` in `[tool.slopfence]` ([#12](https://github.com/syedmuhdahmad/slopfence/issues/12)).

### Added

- `--jobs N` option and `jobs` config key: files are read, parsed and checked in up to `N` worker processes. Default: one per CPU, at most 8. The report is the same with any number of jobs. If worker processes can't start, a warning is printed and the files are checked in one process ([#12](https://github.com/syedmuhdahmad/slopfence/issues/12))

### Changed

- **Much faster on big projects, with far less memory.** The CPython standard library and 127 installed packages (22,864 files) took 5 min 44 s and 8 GB of memory in v0.2.0. Now they take a little over 2 minutes with one process, and 35 to 45 seconds with 8 jobs on 4 cores. The main process needs about 0.3 GB, and each worker 40 to 80 MB ([#12](https://github.com/syedmuhdahmad/slopfence/issues/12))
- Checking a few files of a big project (pre-commit) is faster. The rest of the project is only read for what `SLOP030` needs, and its test files are not read at all ([#12](https://github.com/syedmuhdahmad/slopfence/issues/12))
- PyPI lookups are sent 8 at a time instead of one by one. 200 uncached names took more than 90 seconds and now take about 10. What is sent has not changed. A name is requested once per run, also when PyPI gives no answer for it ([#54](https://github.com/syedmuhdahmad/slopfence/issues/54))
- The PyPI cache file is written in one step, so two runs at the same time can't leave a broken file ([#54](https://github.com/syedmuhdahmad/slopfence/issues/54))
- Ctrl-C during a scan prints `slopfence: interrupted` and exits with status `130`, instead of a traceback. On Windows, worker processes that are still starting can print tracebacks of their own ([#12](https://github.com/syedmuhdahmad/slopfence/issues/12))

### Fixed

- Findings at the same position (`import a, b` where neither exists) could be listed in a different order from run to run. They are now ordered by message ([#12](https://github.com/syedmuhdahmad/slopfence/issues/12))
- A file that Python only warns about (an invalid escape sequence such as `"\d"`) counted as unparseable when warnings were turned into errors (`python -W error`). Such a file is now always parsed, and Python's warning about it is no longer printed ([#12](https://github.com/syedmuhdahmad/slopfence/issues/12))

## [0.2.0] - 2026-10-03

Three new rules, private package support, and safer defaults for private code.

### Upgrade notes

- **Imports are no longer looked up on PyPI by default** ([#34](https://github.com/syedmuhdahmad/slopfence/issues/34)). Dependency files (`requirements*.txt`, `pyproject.toml`) are still checked as before. To also check imports in source code, as v0.1 did, pass `--check-imports` or set `check-imports = true` in `[tool.slopfence]`. See "What slopfence sends over the network" in the README.
- **If pip or uv uses a private index** (`PIP_INDEX_URL`, `pip.conf`, `uv.toml`…), `SLOP001` now makes no PyPI lookups, and the report says so in a note ([#17](https://github.com/syedmuhdahmad/slopfence/issues/17)). If that index only mirrors PyPI, set `detect-private-index = false`.
- Three new rules (`SLOP011`, `SLOP030`, `SLOP040`) are on by default. To roll them out gradually, use `ignore = ["SLOP030"]`, `--diff`, or `--fail-on medium` (SLOP040 is low severity).

### Added

- **New rules:**
  - `SLOP011` stub functions: a docstring that promises work ("Validate the token") over a body that returns hardcoded data or nothing ([#22](https://github.com/syedmuhdahmad/slopfence/issues/22))
  - `SLOP030` duplicate functions: identical functions across the project, ignoring names and docstrings. With `--diff`, changed functions are also reported when they're ≥90% similar to an existing one ([#11](https://github.com/syedmuhdahmad/slopfence/issues/11))
  - `SLOP040` swallowed exceptions: `except Exception: pass` and bare `except: pass` in application code (low severity) ([#21](https://github.com/syedmuhdahmad/slopfence/issues/21))
- **Private packages and indexes:**
  - `known-packages` config and `--known-packages` option: private packages that `SLOP001` accepts without a PyPI lookup, with glob support ([#15](https://github.com/syedmuhdahmad/slopfence/issues/15))
  - Private indexes in the environment are detected: `PIP_INDEX_URL` and the other pip and uv variables, `pip.conf` / `pip.ini`, `uv.toml`, and Pipfile sources. Then `SLOP001` makes no PyPI lookups. New `detect-private-index` config and `--detect-private-index` / `--no-detect-private-index` options ([#17](https://github.com/syedmuhdahmad/slopfence/issues/17))
  - Project-wide private indexes in `pyproject.toml` are detected (`[[tool.poetry.source]]`, `[[tool.uv.index]]`, `[tool.uv]` and `[tool.uv.pip]` `index-url` / `extra-index-url`, `[[tool.pdm.source]]`): that file's dependencies are neither reported nor sent to PyPI ([#34](https://github.com/syedmuhdahmad/slopfence/issues/34))
  - Index URLs are matched by host, never printed (they can contain credentials), and never treated as public just because they contain "pypi.org"
- **Better import checks:** `SLOP001` knows which package provides an import even when the names differ and the package isn't installed (`bs4` from `beautifulsoup4`, `google.cloud` from `google-cloud-storage`), and reads `uv.lock`, `poetry.lock`, `pdm.lock`, `Pipfile.lock` and `Pipfile` for indirect dependencies ([#16](https://github.com/syedmuhdahmad/slopfence/issues/16))
- **Options:**
  - `check-imports` config and `--check-imports` / `--no-check-imports` ([#34](https://github.com/syedmuhdahmad/slopfence/issues/34))
  - `fail-on` config and `--fail-on`: choose which severities fail the run ([#18](https://github.com/syedmuhdahmad/slopfence/issues/18))
  - `strict` config and `--strict` / `--no-strict`: unparseable Python files exit with code 2. Parse errors are also reported in SARIF as tool notifications, and JSON/SARIF use project-relative paths for them ([#19](https://github.com/syedmuhdahmad/slopfence/issues/19))
- **Reports:** notes, e.g. why `SLOP001` skipped lookups. Text output prints them, JSON has a `notes` list, and SARIF has `note` tool notifications
- **Docs:** demo GIF in the README, recorded with VHS from `docs/demo/demo.tape` ([#24](https://github.com/syedmuhdahmad/slopfence/issues/24)), plus the slopfence logo

### Changed

- **Privacy:** `SLOP001` no longer sends import names from your source code to PyPI by default (see the upgrade notes). The README and `SECURITY.md` explain exactly what is sent ([#34](https://github.com/syedmuhdahmad/slopfence/issues/34))
- The source distribution only contains the package, tests and the files needed to build it (about 40 KB instead of 410 KB) ([#35](https://github.com/syedmuhdahmad/slopfence/issues/35))
- Colour output: rule IDs are coloured by severity, locations are dimmed, and the summary counts are coloured. Plain output (`--no-color`, pipes, files) is unchanged

### Fixed

- `SLOP010` false positives from "for demo purposes" in human-written docs. It now needs "only" or placeholder wording nearby, and demo/example code is skipped ([#20](https://github.com/syedmuhdahmad/slopfence/issues/20))

## [0.1.1] - 2026-10-02

### Fixed

- GitHub Action: install into a private virtualenv instead of the runner's system Python, which recent runners reject (PEP 668), and pick a Python 3.10+ interpreter (`python3` or `python`, so it also works on Windows) ([#13](https://github.com/syedmuhdahmad/slopfence/issues/13))
- Crash on Windows when a finding quotes non-ASCII text such as an emoji: output is now always UTF-8. The crash exited with code 1, the same as "issues found" ([#13](https://github.com/syedmuhdahmad/slopfence/issues/13))
- PyPI version and Python badges on the PyPI page ([#9](https://github.com/syedmuhdahmad/slopfence/pull/9))

### Added

- `[tool.slopfence]` configuration in `pyproject.toml` with `select`, `ignore` and `exclude` ([#10](https://github.com/syedmuhdahmad/slopfence/issues/10))
- `--exclude` option
- End-to-end CI tests for the GitHub Action (Linux, macOS, Windows, including SARIF upload to code scanning) and the pre-commit hook ([#13](https://github.com/syedmuhdahmad/slopfence/issues/13), [#14](https://github.com/syedmuhdahmad/slopfence/issues/14))

## [0.1.0] - 2026-10-02

### Added

- Published to PyPI as `slopfence` (the project was originally named slopcheck, but that PyPI name belongs to an unrelated project)
- `slopfence` CLI with `--diff`, `--format text|json|sarif`, `--select`, `--ignore`, `--offline` and `--exit-zero`
- `SLOP001`: dependencies and imports that don't exist on PyPI, with a local cache
- `SLOP010`: placeholder comments left in finished-looking code
- `SLOP020`: tests that only assert on their own mocks
- `SLOP021`: tests whose only assertions are always true
- `SLOP022`: tests whose assertions are swallowed by try/except
- `SLOP051`: leftover AI chat text in comments and docstrings
- `# slopfence: ignore[...]` and `# slopfence: ignore-file` directives
- GitHub Action (`action.yml`) and pre-commit hook

### Project setup

- Project README describing the problem, design principles, planned detectors and roadmap
- Community health files: Code of Conduct, Contributing guide, Security policy, Support, Governance
- Issue templates, pull request template, CODEOWNERS, Dependabot and CI

[Unreleased]: https://github.com/syedmuhdahmad/slopfence/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/syedmuhdahmad/slopfence/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/syedmuhdahmad/slopfence/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/syedmuhdahmad/slopfence/releases/tag/v0.1.0
