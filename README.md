<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/syedmuhdahmad/slopfence/main/docs/brand/logo-dark.svg">
    <img alt="slopfence" src="https://raw.githubusercontent.com/syedmuhdahmad/slopfence/main/docs/brand/logo-light.svg" width="460">
  </picture>
</h1>

[![PyPI](https://img.shields.io/pypi/v/slopfence?cacheSeconds=3600)](https://pypi.org/project/slopfence/)
[![Python](https://img.shields.io/pypi/pyversions/slopfence?cacheSeconds=3600)](https://pypi.org/project/slopfence/)
[![CI](https://github.com/syedmuhdahmad/slopfence/actions/workflows/ci.yml/badge.svg)](https://github.com/syedmuhdahmad/slopfence/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**A fast, deterministic quality gate for AI-assisted code.**

slopfence finds the junk that AI coding assistants leave behind (hallucinated packages, tests that test nothing, placeholder stubs, duplicate helpers, swallowed errors and leftover chat text) before it gets merged.

![slopfence checking an AI-written billing service: it finds a dependency that doesn't exist on PyPI, a placeholder comment, leftover "Certainly!" chat text, and three tests that can't fail](https://raw.githubusercontent.com/syedmuhdahmad/slopfence/main/docs/demo.gif)

> 🚧 **Status: alpha (v0.2, Python only).** Nine detectors work, each tuned against 20,000+ files of real-world code for false positives. Expect rough edges. Feedback is very welcome. See [Contributing](#contributing).

---

## Quick start

```bash
pip install slopfence
```

Or run it without installing, using [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/):

```bash
uvx slopfence .
pipx run slopfence .
```

Check a project:

```bash
slopfence .
```

Check only what your branch changed (ideal for pull requests):

```bash
slopfence --diff main
```

### Options

| Option | What it does |
|---|---|
| `--diff REF` | Only report issues on lines changed since `REF` (plus new untracked files) |
| `--format text\|json\|sarif` | Output format. SARIF shows findings inline on GitHub pull requests |
| `-o, --output FILE` | Write the report to a file |
| `--offline` | Make no network requests (disables `SLOP001`) |
| `--check-imports` / `--no-check-imports` | Also look up unknown imports in your source code on PyPI. Off by default, because it sends those names to pypi.org (see [What slopfence sends over the network](#what-slopfence-sends-over-the-network)) |
| `--select` / `--ignore` | Comma-separated rule IDs to run or skip, e.g. `--ignore SLOP051` |
| `--exclude` | Comma-separated paths or globs to skip, e.g. `--exclude migrations,*_pb2.py` |
| `--known-packages` | Comma-separated private package names or globs that `SLOP001` must accept, e.g. `--known-packages corp-*` |
| `--detect-private-index` / `--no-detect-private-index` | Skip PyPI lookups when pip or uv is set up with a private index (on by default). Turn it off if that index only mirrors PyPI. See [Private package indexes](#private-package-indexes) |
| `--fail-on high\|medium\|low` | Lowest severity that fails the run (default `low`: any issue). All issues are still reported |
| `--baseline FILE` | Hide the findings recorded in `FILE` and report only new ones. See [Baseline: start with the code you have](#baseline-start-with-the-code-you-have) |
| `--write-baseline FILE` | Record the current findings in `FILE`. They are not reported, and later runs with `--baseline FILE` hide them |
| `--no-baseline` | Ignore the `baseline` set in the config and report every finding |
| `--strict` / `--no-strict` | Exit `2` if any Python file can't be parsed (by default it's skipped with a warning) |
| `--jobs N` | Check files in up to `N` processes. Default: one per CPU, at most 8. `--jobs 1` uses a single process. See [Speed](#speed) |
| `--exit-zero` | Always exit 0 (report only) |
| `--list-rules` | Show all rules with the severities this project uses |

Exit codes: `0` no issues (or none at or above `--fail-on`, or none that the baseline doesn't hide), `1` issues found, `2` error (bad options or config, a baseline file that is missing, not valid or can't be written, or unparseable files with `--strict`), `130` stopped with Ctrl-C.

Run slopfence with a Python at least as new as the syntax your code uses; otherwise newer syntax can't be parsed.

### Configuration

Add a `[tool.slopfence]` table to the `pyproject.toml` at your project root:

```toml
[tool.slopfence]
select = ["SLOP001", "SLOP010", "SLOP020"]  # rules to run (default: all)
ignore = ["SLOP051"]                         # rules to skip
exclude = ["migrations", "tests/fixtures/", "*_pb2.py"]
known-packages = ["corp-auth", "corp-*"]    # private packages that aren't on PyPI
check-imports = false                        # true also looks up imports on PyPI (default: false)
detect-private-index = true                  # false if your private index only mirrors PyPI
fail-on = "high"                             # only high-severity issues fail the run
strict = true                                # unparseable files are an error
jobs = 4                                     # worker processes (default: one per CPU, at most 8)
baseline = ".slopfence-baseline.json"       # hide the findings recorded in this file

[tool.slopfence.per-file-ignores]            # rules to skip in some files
"scripts/*" = ["SLOP040"]
"legacy/" = ["SLOP030", "SLOP011"]

[tool.slopfence.severity]                    # give a rule another severity
SLOP040 = "medium"
```

`exclude` works like `.gitignore`: a pattern without a `/` in the middle matches any file or folder name anywhere (`migrations`, `*_pb2.py`), and a pattern with one is relative to the project root (`tests/fixtures/`). Excluded files are never checked, but imports of excluded modules still count as your own code.

`per-file-ignores` turns rules off in the files that match a pattern. A pattern works exactly like an `exclude` pattern. Put it in quotes. If several patterns match a file, all their rules are skipped. Unlike `exclude`, every other rule still checks the file. Two details:

- Dependencies and imports in a file where `SLOP001` is off are not looked up on PyPI at all.
- `SLOP030` is off for the functions *in* a matching file. Code anywhere else that copies a helper from `legacy/` is still reported, whichever of the two is older. With `--diff`, it is only reported when its own lines changed. So a change that only adds a copy of your code *inside* `legacy/` passes a `--diff` run, and the next full run reports the older code outside the folder.

`severity` gives a rule another severity: `high`, `medium` or `low`. Text, JSON and SARIF reports, `--fail-on` and `--list-rules` all use it. For example, with `SLOP040 = "high"` a swallowed exception fails a run that uses `--fail-on high`.

`baseline` is explained in [Baseline: start with the code you have](#baseline-start-with-the-code-you-have).

Command-line options (`--select`, `--ignore`, `--exclude`, `--known-packages`, `--check-imports`, `--detect-private-index`, `--fail-on`, `--strict`, `--jobs`, `--baseline`) replace the matching config values. `per-file-ignores` and `severity` can only be set in the config. Unknown keys, rule IDs and severities are reported as errors (exit code `2`).

### Ignoring a finding

```python
# In a real implementation, use the cache.  # slopfence: ignore[SLOP010]
x = legacy()  # slopfence: ignore
```

Put `# slopfence: ignore-file` anywhere in a file to skip it. In `requirements.txt`, add `# slopfence: ignore` to a line.

To skip a rule in whole files or folders, use [`per-file-ignores`](#configuration). To accept everything a project has today and only see new findings, use a [baseline](#baseline-start-with-the-code-you-have).

**Private packages:** if your project uses internal packages that aren't on PyPI, list them once in `known-packages` instead of adding ignore comments everywhere. Names are matched case-insensitively with `-`, `_` and `.` treated alike (so `corp_auth` matches `corp-auth`), and `*` globs work. Known packages are never looked up on PyPI, and imports of them are never flagged.

`SLOP010` (placeholders) also skips test code and demo or example code: files under `demo/`, `examples/` or `samples/` folders, or named like `demo.py` or `auth_example.py`.

### Baseline: start with the code you have

An existing project may have hundreds of findings. A baseline records them once, so slopfence only reports what is new:

```bash
slopfence --write-baseline .slopfence-baseline.json   # record today's findings
git add .slopfence-baseline.json
slopfence --baseline .slopfence-baseline.json          # from now on: new findings only
```

Or set it once in `pyproject.toml`. Then every run, the pre-commit hook and the GitHub Action use it:

```toml
[tool.slopfence]
baseline = ".slopfence-baseline.json"
```

With a baseline, only new findings are reported, and only new findings fail the run. The summary says how many were hidden:

```text
1 issue (1 medium); 42 more hidden by the baseline
```

**What is recorded.** The file has one line per finding:

```json
["src/auth.py", "SLOP011", "validate_token", 1]
```

That is the file, the rule, what the finding is about, and how many there are. "What it is about" is the package name, the function or test name, or the text of the flagged comment. There is no line number. So a finding stays hidden when you add lines above it, change the code around it, or reformat the file.

**When a finding comes back.** When you rename the function or the test, reword the comment, or rename or move the file. slopfence then sees a new finding.

**Good to know:**

- It works with `--diff`: only findings on changed lines are reported, and baselined ones stay hidden. It works with `--fail-on` and with every output format. JSON reports have a `baselined` count. SARIF reports leave baselined findings out.
- Two findings in one file that are about the same thing (two `except Exception: pass` in one function) are counted. If a third appears, one is reported: the one on your changed lines with `--diff`, otherwise the last one in the file.
- Duplicate functions (`SLOP030`): a full run reports every copy except the first, so the first copy has no line in the file. It is accepted all the same. With `--diff`, editing either copy does not bring the pair back, also when the two are only 90% alike afterwards. (If every line of the first copy changes, it can count as a new function.) Near-copies are only looked for with `--diff`, so a baseline never records one. A near-copy that a change adds is reported. And when you write the baseline again, a pair that is no longer identical drops out of it. A later change to one of the two is then reported, as long as they are still 90% alike. To keep a near-copy, add `# slopfence: ignore[SLOP030]` to its `def` line. A copy added to a file where `SLOP030` is switched off is not reported by a `--diff` run: see [`per-file-ignores`](#configuration).
- When baselined findings are gone, slopfence says so in a note. Run `--write-baseline` again to drop them from the file. The note only comes when the run checks everything the baseline holds. So it never comes with `--diff`, and not when the paths you give, or `exclude`, `select` and `ignore`, leave out files or rules that the baseline has findings for. It never counts `SLOP001`: a missing answer from PyPI must not look like a fix.
- `--write-baseline` records every finding of that run and reports none. Use the options your normal runs use (`--select`, `--check-imports`…). Run it online: without answers from PyPI there are no `SLOP001` findings to record, and a note says so. It can't be combined with `--diff`.
- `--write-baseline` only replaces a baseline. It stops if `FILE` is some other file. It also stops if `FILE` holds findings that this run does not check: for files outside the paths you give, for files you exclude, or for rules that are switched off. Check everything the baseline holds, or delete `FILE` first.
- `baseline` in the config is relative to the project root. `--baseline FILE` is relative to the folder you are in, and it replaces the config value. `--no-baseline` shows everything again.
- The paths inside the file are relative to the project root. That is the git repository the checked path is in. Without git, it is the folder you run slopfence from (or the checked folder, when that is somewhere else), so run slopfence the same way every time. If no file that the baseline names exists there, slopfence says so in a note, and `--write-baseline` does not replace the file.
- A file outside the project (`slopfence . ../shared`) is recorded with its full path. That row only matches on a machine where the file has the same path. slopfence only looks at such a file when you give its path to the run.
- A baseline file that is missing or can't be read is an error (exit code `2`), not an empty baseline.
- Files that can't be parsed are not findings, so they can't be baselined. Use `exclude` for those.

### What slopfence sends over the network

slopfence only talks to one server, `https://pypi.org`, and only for `SLOP001`. It sends a request per package name (`HEAD https://pypi.org/pypi/<name>/json`) and nothing else: no code, no file names, no telemetry. Up to 8 requests are sent at a time. That changes how fast the answers arrive, not what is sent.

| Names sent to PyPI | When |
|---|---|
| Dependencies declared in `requirements*.txt` and `pyproject.toml` | By default. These names are already meant for a package index. Entries installed from URLs, paths or git are never sent. Neither is any dependency in a file that uses a private index: `--index-url` / `--extra-index-url` in a requirements file, a `[[tool.poetry.source]]` or `[[tool.uv.index]]` that isn't PyPI (unless it's `explicit`, which only affects the dependencies that name it), or a per-dependency Poetry `source` or `[tool.uv.sources]` entry |
| Imports in your source code that aren't stdlib, installed, part of your project, or provided by a declared or locked package | **Only with `--check-imports`** (or `check-imports = true`). This can catch an invented package that was imported but never declared, but for private code it may reveal internal package names |

Names in `known-packages` are never sent. Neither are the dependencies and imports of a file where `SLOP001` is switched off with `per-file-ignores`, or a dependency or import under an [ignore comment](#ignoring-a-finding) (`# slopfence: ignore` on its line, or `# slopfence: ignore-file`). Answers are cached for a day (missing) or a week (found) in `~/.cache/slopfence`, so repeated runs send fewer requests. `--offline` sends nothing at all.

### Private package indexes

If your packages may come from an index other than PyPI, a name that PyPI doesn't know could be a private package. slopfence then doesn't report it and doesn't send it to PyPI. It looks for private indexes in:

| Where | What counts as private |
|---|---|
| `requirements*.txt` | `-i` / `--index-url` / `--extra-index-url` with a non-PyPI URL: that file's packages |
| `pyproject.toml` | `[[tool.poetry.source]]` (except `explicit` ones), `[[tool.uv.index]]` (except `explicit` ones), `[tool.uv] index-url` / `extra-index-url` (also under `[tool.uv.pip]`), `[[tool.pdm.source]]`: that file's packages. A per-dependency Poetry `source` or `[tool.uv.sources]` entry: that package |
| `Pipfile`, `Pipfile.lock` | A `[[source]]` that isn't PyPI |
| Environment variables | `PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`, `UV_INDEX`, `UV_DEFAULT_INDEX`, `UV_INDEX_URL`, `UV_EXTRA_INDEX_URL` |
| pip configuration | `pip.conf` / `pip.ini` in pip's global, user and virtualenv locations, and `PIP_CONFIG_FILE` (`[global]` and `[install]` sections) |
| uv configuration | `uv.toml` in the project, user and system config folders, or `UV_CONFIG_FILE` (`UV_NO_CONFIG` turns this off), including its `[pip]` table used by `uv pip install` |

A private index in a project file stops lookups for that file's dependencies, and for imports with `--check-imports`. A private index in your **environment** (variables, pip or uv configuration) could serve any package, so `SLOP001` makes **no lookups at all** and the report says so in a note. An index is public only if its host is `pypi.org` or `pythonhosted.org`. slopfence never prints index URLs, because they can contain credentials.

If your company index only **mirrors** PyPI, set `detect-private-index = false` (or pass `--no-detect-private-index`) to check packages against PyPI again, and list your own private packages in `known-packages`.

### Imports with a different package name

With `--check-imports`, an import isn't looked up when a declared dependency or a locked package provides it, even if the names differ and the package isn't installed where slopfence runs. For example, `import bs4` is covered by `beautifulsoup4`, `import dateutil` by `python-dateutil`, and `from google.cloud import storage` by `google-cloud-storage`. slopfence reads `uv.lock`, `poetry.lock`, `pdm.lock`, `Pipfile.lock` and `Pipfile`, so indirect dependencies count too (`import idna` when only `requests` is declared).

### What the code-quality rules skip

To keep false positives low, the newer rules are deliberately conservative:

- **`SLOP011` stub functions** needs a docstring that promises work (it starts with a verb like *validate*, *fetch*, *send*, *calculate*) or admits it isn't done (*placeholder*, *TODO*, *for now*). The body must then return hardcoded data while ignoring every input, or return nothing although the return type promises a value. A placeholder docstring over a body that does nothing (`pass`, `...`, `return None`) is reported even without a return type. `.pyi` stub files are never checked. Abstract methods, `@overload`, properties, `Protocol`/ABC classes, `if TYPE_CHECKING:` blocks, documented hooks ("override this", "does nothing by default") and tests are skipped.
- **`SLOP030` duplicate functions** ignores functions with fewer than 3 statements and test code. Parameter and variable names and docstrings don't matter; called functions, attributes and constants do. A full scan reports **identical** copies (every copy except the first). "The first" is the first by path, and files where `SLOP030` is switched off with `per-file-ignores` always count as first. With `--diff`, functions you changed are also compared with the rest of the project and reported when they're **≥90% similar** to an existing function, because that's when an assistant re-writes a helper that already exists. Never reported: methods with the same name in different classes (plugins, drivers), functions defined twice in one file (under `if`/`try`), near-duplicate methods, and parallel families that differ only by swapped names, operators or constants (`md5_utf8`/`sha_utf8`, `polyadd`/`polysub`).
- **`SLOP040` swallowed exceptions** only flags `except Exception`, `except BaseException` and bare `except:` whose body is just `pass` or `...`. A comment in the handler (`# best effort`), cleanup code (`__del__`, `__exit__`, `close()`, `atexit` handlers, `try: os.unlink(...)`, also in a loop), optional imports (`try: import ujson`), `contextlib.suppress`, tests and examples are skipped.

### Speed

slopfence reads and checks files in several processes at once. The report is the same with any number of them.

- By default it uses one process per CPU, at most 8. Change it with `--jobs N` or `jobs = N`. `--jobs 1` uses a single process.
- Small projects don't need it. Worker processes only start when there are 200 or more Python files to read, one for every 100 files.
- Each worker needs 40 to 80 MB of memory on a big project. In a container with little memory, set `jobs = 1` or `2`.
- If worker processes can't start, or one dies, slopfence prints a warning and checks the files in one process.
- If slopfence is stopped or killed, its worker processes end too.
- PyPI lookups are sent 8 at a time.

Example: the CPython standard library and 127 installed packages (22,864 files) take 35 to 45 seconds with 8 jobs on a laptop with 4 cores, and a little over 2 minutes with one process. v0.2 needed almost 6 minutes and 8 GB of memory for the same files. Now the main process needs about 0.3 GB.

### GitHub Action

```yaml
# .github/workflows/slopfence.yml
name: slopfence
on: pull_request
permissions:
  contents: read
jobs:
  slopfence:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - uses: syedmuhdahmad/slopfence@v0.2.0
```

On pull requests it automatically checks only the changed lines (set `diff: none` to check everything, or `diff: <ref>` for a specific ref). To show findings inline on the pull request, set `sarif-file: slopfence.sarif` and upload it with `github/codeql-action/upload-sarif` (needs `security-events: write`).

A `baseline` in your `pyproject.toml` applies here too: baselined findings are not reported, and they are left out of the SARIF file.

### pre-commit

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/syedmuhdahmad/slopfence
    rev: v0.2.0
    hooks:
      - id: slopfence
```

The hook reads the same `[tool.slopfence]` config, so `per-file-ignores`, `severity` and `baseline` apply to it too.

## The problem

AI coding tools (Claude, Copilot, Cursor, Gemini, Codex and others) are now part of everyday development. They write code fast, and that code usually *looks* correct. It compiles, it's well formatted, and it has comments and tests.

But it often comes with recognizable junk:

- **Packages that don't exist.** The AI imports `flask-jwt-simple-auth` because the name sounds plausible. Attackers now register these invented names to spread malware, a technique known as **slopsquatting**.
- **Tests that test nothing.** A test creates a mock, sets a value on it, then asserts that value. It passes forever and protects nothing.
- **Placeholder code shipped as real code.** Comments like `# In a real implementation, validate the token` above a function that returns `True`.
- **Duplicate helpers.** `format_date()`, `formatDate()` and `date_formatter()` in three different files, because the AI wrote a new helper instead of finding the existing one.
- **Over-defensive code.** `try/except Exception: pass` around code that can't fail, which hides real errors.
- **Comment noise.** `# Increment counter by 1` above `counter += 1`, or leftover chat text like *"Here's the updated function:"*.

Reviewers miss these because the code *looks* fine. Each one is small. Together they add up to security holes, false confidence from green test suites, and codebases that get harder to maintain every sprint.

## Why existing tools aren't enough

| Tool | What it's good at | What it misses |
|---|---|---|
| **Ruff, ESLint, Pylint** | Style and common bugs | AI-specific patterns, invented packages |
| **Vulture, Knip** | Dead code | Fake tests, placeholders, near-duplicates |
| **jscpd** | Copy-pasted code | Code that's similar but not identical |
| **Socket, Snyk** | Supply-chain security | Code-quality patterns; mostly paid |
| **[slopcheck](https://github.com/0xToxSec/slopcheck)** (unrelated project) | Checking for hallucinated packages before you install them | Code-quality patterns such as fake tests, placeholders and chat leftovers |
| **AI code reviewers** (e.g. Claude `/code-review`) | Deep, context-aware review | Not deterministic, costs tokens, doesn't automatically check package registries, runs only when someone asks |

No single tool focuses on **the failure modes of AI-written code**, built for reviewing pull requests. That's the gap slopfence fills.

### slopfence vs. AI reviewers: complementary, not competing

```text
Developer + AI writes code
        │
        ▼
slopfence            ← fast, free, same result every time; runs on every commit
        │
        ▼
AI / LLM review      ← deep reasoning about logic and design
        │
        ▼
Human review
```

AI reviewers are smart but nondeterministic and cost money on every run. slopfence catches the objective, repeatable problems in seconds with no API keys, so reviewers (human or AI) can spend their attention on what matters.

## Who needs it

- **Teams adopting AI coding tools** that want a safety net without banning the tools.
- **Open-source maintainers** reviewing a growing number of AI-generated pull requests.
- **Security teams** worried about hallucinated or slopsquatted dependencies.
- **Tech leads** who want an objective quality gate instead of arguing over style in review.
- **Solo developers** who want to catch their AI assistant's mistakes before they commit.

## Design principles

1. **Detect patterns, not authorship.** slopfence never tries to guess *whether AI wrote the code*. That's unreliable and leads to arguments. It flags low-quality patterns that AI commonly produces, no matter who wrote them.
2. **Deterministic by default.** Rule-based checks with no LLM required. The same input always gives the same output.
3. **Diff-first.** Check only what changed in a pull request. Nobody wants 4,000 warnings on an old codebase.
4. **Low false positives over high recall.** A noisy linter gets uninstalled. Start strict, flag only obvious cases, and make everything configurable.
5. **Fits existing workflows.** CLI, pre-commit hook, GitHub Action, and SARIF output for GitHub code scanning.

## Detectors

| ID | Detector | Severity | Status |
|---|---|---|---|
| `SLOP001` | Dependency (or, with `--check-imports`, import) of a package that doesn't exist on PyPI | 🔴 High | ✅ v0.1 (Python) |
| `SLOP002` | Dependency that is very new or has suspiciously few downloads (slopsquatting risk) | 🔴 High | Planned |
| `SLOP010` | Placeholder / stub comment (`In a real implementation…`, `Simplified for demo`) | 🟠 Medium | ✅ v0.1 |
| `SLOP011` | Function whose docstring promises work ("Validate the token") but which only returns hardcoded data or nothing | 🟠 Medium | ✅ v0.2 |
| `SLOP020` | Test that only asserts on its own mocks | 🔴 High | ✅ v0.1 |
| `SLOP021` | Test with no meaningful assertion (e.g. only `assert True`) | 🟠 Medium | ✅ v0.1 |
| `SLOP022` | Test wrapped in `try/except` so it can never fail | 🔴 High | ✅ v0.1 |
| `SLOP030` | Function identical to another one in the project (names and docstrings ignored); with `--diff`, also new functions ≥90% similar to existing ones | 🟠 Medium | ✅ v0.2 |
| `SLOP040` | `except Exception` (or bare `except`) that silently swallows every error | 🟡 Low | ✅ v0.2 |
| `SLOP050` | Comment that only restates the code | 🟡 Low | Planned ([#23](https://github.com/syedmuhdahmad/slopfence/issues/23)) |
| `SLOP051` | Leftover chat text in code (`Certainly! Here's…`) | 🟠 Medium | ✅ v0.1 |

## Example output

```text
$ slopfence --diff main
requirements.txt
  2:1      SLOP001  Dependency 'flask-jwt-simple-auth' does not exist on PyPI (possible hallucination)  [high]

src/auth.py
  5:5      SLOP010  Placeholder left in code: "In a real implementation, validate the token signature."  [medium]

tests/test_billing.py
  7:5      SLOP022  Test 'test_refund' catches assertion failures without re-raising, so it can never fail  [high]

tests/test_user.py
  4:1      SLOP020  Test 'test_user_name' only asserts on its own mocks and never calls real code  [high]

4 issues (3 high, 1 medium)
```

## Architecture

```text
┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌───────────────┐
│   Files /   │──▶│    Parser    │──▶│  Rule engine │──▶│   Reporter    │
│   git diff  │   │ (ast / t-s)  │   │  (detectors) │   │ CLI/JSON/SARIF│
└─────────────┘   └──────────────┘   └──────┬───────┘   └───────────────┘
                                            │
                                  ┌─────────▼─────────┐
                                  │ Optional:         │
                                  │ - registry lookup │
                                  │   (cached)        │
                                  │ - LLM second      │
                                  │   opinion (--llm) │
                                  └───────────────────┘
```

- **Parser:** slopfence uses Python's built-in `ast` and `tokenize` (no dependencies). **tree-sitter** is planned so new languages can be added without rewriting the detectors.
- **Workers:** files are parsed and checked in worker processes when there are enough of them (see [Speed](#speed)). Only a small summary of each file comes back: its findings, function fingerprints and import names. So memory stays low. Registry lookups stay in the main process.
- **Registry lookups** are cached locally (`~/.cache/slopfence`) to stay fast and avoid rate limits. If PyPI can't be reached, nothing is flagged: an unknown answer is never treated as "missing". Import names are only sent with `--check-imports` (see [What slopfence sends over the network](#what-slopfence-sends-over-the-network)).
- **Optional LLM mode** (planned, not built yet) would send only the unclear cases for a second opinion. Never required.

## Challenges (and how we plan to handle them)

| Challenge | Why it's hard | Plan |
|---|---|---|
| **False positives** | Every noisy warning erodes trust; one bad week and teams disable the tool. | Conservative rules, severity levels, `# slopfence: ignore[SLOP0xx]` comments, per-file ignores, a baseline for the findings a project already has, and a test corpus of real-world false positives. |
| **Private / internal packages** | Internal packages look identical to hallucinated ones. | Allowlists, private registry support, and reading local lockfiles and workspace packages. |
| **Registry rate limits** | Checking every import against PyPI/npm on every run is slow and gets throttled. | Local cache with TTL, batch lookups, offline mode. |
| **"Is this really junk?"** | Some patterns (defensive code, stubs) are legitimate in context. | Flag only high-confidence cases by default; stricter checks are opt-in. |
| **Near-duplicate detection at scale** | Comparing every function with every other is expensive. | AST normalization + hashing, and only comparing changed functions against the index. |
| **Multi-language support** | Each language has different idioms and test frameworks. | Start with Python only, do it well, then JS/TS via tree-sitter. |
| **Tone and adoption** | A tool that "shames AI users" will be resisted. | Position it as a quality gate *for* AI-assisted development, not against it. |
| **Moving target** | AI tools improve and their failure modes change. | Rules are data-driven and easy to add; community-contributed detectors. |

## Roadmap

### v0.1 (MVP): Python

- [x] CLI: `slopfence .` and `slopfence --diff <branch>`
- [x] `SLOP001` packages that don't exist on PyPI (dependency files and imports)
- [x] `SLOP010` placeholder comments
- [x] `SLOP020` / `SLOP021` / `SLOP022` fake tests
- [x] `SLOP051` leftover chat text
- [x] Ignore comments
- [x] JSON and SARIF output
- [x] GitHub Action and pre-commit hook
- [x] Publish to PyPI

### v0.2: private code, new rules ([released](https://github.com/syedmuhdahmad/slopfence/releases/tag/v0.2.0))

Tracked in the [v0.2.0 milestone](https://github.com/syedmuhdahmad/slopfence/milestone/2). Upcoming work is on the [roadmap board](https://github.com/users/syedmuhdahmad/projects/1).

#### Planned features

- [x] Config file: `[tool.slopfence]` in `pyproject.toml` ([#10](https://github.com/syedmuhdahmad/slopfence/issues/10))
- [x] `SLOP030` near-duplicate functions ([#11](https://github.com/syedmuhdahmad/slopfence/issues/11))

#### Reliability

- [x] Test the GitHub Action in a real workflow, including SARIF upload ([#13](https://github.com/syedmuhdahmad/slopfence/issues/13))
- [x] Test the pre-commit hook with `pre-commit` ([#14](https://github.com/syedmuhdahmad/slopfence/issues/14))
- [x] Allowlist for private packages ([#15](https://github.com/syedmuhdahmad/slopfence/issues/15))
- [x] Better import-name to package-name mapping ([#16](https://github.com/syedmuhdahmad/slopfence/issues/16))
- [x] Detect private indexes from `pip.conf`, uv and Poetry ([#17](https://github.com/syedmuhdahmad/slopfence/issues/17))
- [x] `--fail-on` severity threshold ([#18](https://github.com/syedmuhdahmad/slopfence/issues/18))
- [x] Option to fail on unparseable files ([#19](https://github.com/syedmuhdahmad/slopfence/issues/19))
- [x] Fewer `SLOP010` false positives from "for demo purposes" ([#20](https://github.com/syedmuhdahmad/slopfence/issues/20))
- [x] Import lookups are opt-in, so private package names aren't sent to PyPI ([#34](https://github.com/syedmuhdahmad/slopfence/issues/34))
- [x] Smaller source distribution ([#35](https://github.com/syedmuhdahmad/slopfence/issues/35))

#### New rules

- [x] `SLOP040` `except Exception` that silently swallows errors ([#21](https://github.com/syedmuhdahmad/slopfence/issues/21))
- [x] `SLOP011` stub functions that only `pass` or return fake data ([#22](https://github.com/syedmuhdahmad/slopfence/issues/22))

#### Project

- [x] Demo GIF in the README ([#24](https://github.com/syedmuhdahmad/slopfence/issues/24))
- [x] Test coverage measured in CI ([#48](https://github.com/syedmuhdahmad/slopfence/issues/48))
- [x] Release v0.2.0 ([#25](https://github.com/syedmuhdahmad/slopfence/issues/25))

### Later

- [ ] `SLOP050` comments that only restate the code ([#23](https://github.com/syedmuhdahmad/slopfence/issues/23))
- [x] Parallel processing for large repos ([#12](https://github.com/syedmuhdahmad/slopfence/issues/12))
- [x] Baseline mode: fail only on new findings ([#51](https://github.com/syedmuhdahmad/slopfence/issues/51))
- [x] Per-file ignores and per-rule severity overrides ([#52](https://github.com/syedmuhdahmad/slopfence/issues/52))
- [ ] JavaScript / TypeScript (npm registry)
- [ ] Slopsquatting risk scoring (`SLOP002`)
- [ ] VS Code extension
- [ ] Optional `--llm` second opinion

## Contributing

The project is at the very start, which is the best time to shape it.

- 💡 **Have an example of AI-generated junk?** Open an issue with a code snippet. Real examples are the most valuable input for designing detectors.
- 🐛 **Know a pattern we're missing?** Propose a detector in an issue.
- 🗣️ **Disagree with the approach?** Open a discussion. Better now than after v1.

Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Everyone taking part is expected to follow our [Code of Conduct](CODE_OF_CONDUCT.md). Found a security issue? Report it privately as described in [SECURITY.md](SECURITY.md). Need help? See [SUPPORT.md](SUPPORT.md).

## License

[MIT](LICENSE)
