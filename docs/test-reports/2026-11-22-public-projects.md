# Test report: slopfence on 18 public GitHub projects

Date: 2026-11-22. Machine: Linux 7.0, 8 CPUs, Python 3.14.4, git 2.53.

This report says what happened when slopfence checked 18 well-known public projects, before and after a set of fixes. It also lists what the run found that is still open.

## Summary

- **46,747 files in 18 projects, no crash.** Both builds finished every project. Exit codes were `0` or `1`, never `2`.
- **The fixes change no finding on real code, except the one they are meant to change.** 17 of 18 projects have the same findings on `main` and on the fixed build. The 18th, AutoGPT, loses nine `SLOP051` findings that were all wrong (see [What the run found](#what-the-run-found)).
- **`SLOP001` reported no package as missing.** 1,782 package names from the 18 projects were looked up on PyPI. Every one exists.
- **`--diff` reads real history the same way as before.** 1,735 files and 177,734 changed lines from the last 25 commits of each project: the old and the new reader agree on every file.
- **The run found two bugs that no issue named.** Both are fixed on branches of their own, with tests.
- **Speed did not change:** 96.3 s on `main`, 95.7 s on the fixed build, for all 18 projects.
- **Most findings on these projects are not junk.** These are mature, reviewed projects. Of 30 findings read by hand, 2 were useful, 17 were right but on purpose, and 11 were wrong. The numbers are in [Findings read by hand](#findings-read-by-hand).

## What was tested

Two builds of slopfence:

| Build | Commit | What it is |
|---|---|---|
| `main` | `da6cf3f` | `origin/main` on the day of the test |
| fixed | `ae4c974` | `main` with the nine branches below merged |

| Branch | Commit | Closes | What it fixes |
|---|---|---|---|
| `fix/crash-exit-code` | `062b7e6` | #67 | A crash inside slopfence exits with `2` and one line, not with `1` and a traceback |
| `fix/cache-file-mode` | `fddb3bb` | #70 | The PyPI cache keeps its permissions. A new cache folder is for its owner only. Temporary files can't be hijacked through a link |
| `fix/start-git-by-full-path` | `5c03f87` | #92 | git is started by its full path, never from the checked folder. git runs read-only and without the repository's file monitor |
| `fix/project-root-without-git` | `ef531a1` | #69 | Without git, `[tool.slopfence]` is found from any start folder |
| `fix/diff-deleted-lines` | `9fb7eea` | #71 | `--diff` sees a function that became a copy by losing lines. A diff is read by its hunk counts. Files with a space in the name work |
| `fix/hardening-for-untrusted-projects` | `f3f20a5` | | A link out of the project is not read. Pipes and devices are not opened. The text report writes control characters out |
| `fix/large-notebook-cells` | `9127128` | | A notebook cell saved as one long string is checked in linear time |
| `fix/as-an-ai-false-positive` | `c82ec21` | | `SLOP051` no longer reports "as an AI SDK" (found in this run) |
| `fix/comments-under-warning-filters` | `b4bead6` | | Comments are read whatever the warning filters are (found in this run) |

`fix/diff-deleted-lines` is built on `fix/start-git-by-full-path`. The other branches start from `main`.

The tables below come from a run of the fixed build at `5d65483`, which is `ae4c974` without the last branch. `psf/black`, the one project that branch changes anything for, was run again at `ae4c974`.

## The projects

Each project was cloned from GitHub with `git clone --depth 30` on 2026-11-22.

| Project | Commit | Commit date | Python files | Notebooks |
|---|---|---|---:|---:|
| `ansible/ansible` | `f0d3af989` | 2026-11-19 | 1,863 | 0 |
| `celery/celery` | `a2d470f19` | 2026-11-19 | 454 | 0 |
| `django/django` | `0eae0b258` | 2026-11-21 | 2,943 | 0 |
| `encode/httpx` | `7cff5f79b` | 2026-11-16 | 60 | 0 |
| `fastapi/fastapi` | `05e6df7e9` | 2026-11-18 | 1,233 | 0 |
| `home-assistant/core` | `68fb29a09` | 2026-11-21 | 20,655 | 0 |
| `huggingface/transformers` | `2f1bc5e32` | 2026-11-21 | 5,205 | 0 |
| `jakevdp/PythonDataScienceHandbook` | `d66231454` | 2023-05-05 | 10 | 136 |
| `langchain-ai/langchain` | `8ff3cd30e` | 2026-11-20 | 2,672 | 0 |
| `pallets/flask` | `49943b5a9` | 2026-10-30 | 83 | 0 |
| `pandas-dev/pandas` | `65e672976` | 2026-11-21 | 1,555 | 1 |
| `psf/black` | `a12e95913` | 2026-11-19 | 451 | 6 |
| `psf/requests` | `3c7baaa05` | 2026-10-26 | 37 | 0 |
| `pydantic/pydantic` | `1aaef0180` | 2026-11-21 | 3,163 | 0 |
| `scikit-learn/scikit-learn` | `21626d67d` | 2026-11-21 | 1,039 | 0 |
| `scrapy/scrapy` | `00acf3f31` | 2026-11-19 | 557 | 0 |
| `Significant-Gravitas/AutoGPT` | `dd0b5f732` | 2026-11-20 | 4,230 | 0 |
| `Textualize/rich` | `3a109ccdf` | 2026-11-07 | 212 | 0 |

## How it was run

Every project was checked with the command line, three times:

```bash
python -I -m slopfence --format json --offline <project>              # main
python -I -m slopfence --format json --offline <project>              # fixed
python -I -m slopfence --format json --select SLOP001,SLOP002 <project>   # fixed, with PyPI
```

- slopfence was started in an empty folder outside the project, with `python -I`. No file of a project was run or imported.
- The findings of the two offline runs were compared as sets of rule, path, line, column and message.
- The run with PyPI used a cache folder of its own, empty at the start.
- For `--diff`, each project was checked with `--offline --diff HEAD~25` on both builds. The raw output of `git diff` was also given to the diff reader of each build, and the results were compared file by file.

## Results

### Full runs, offline

"Files checked" counts Python files that parse and dependency files. Times are for one run, with the default number of worker processes.

| Project | Files checked | Findings | Time, `main` | Time, fixed | Same findings |
|---|---:|---:|---:|---:|---|
| `ansible/ansible` | 1,910 | 37 | 2.3 s | 2.3 s | yes |
| `celery/celery` | 506 | 4 | 1.7 s | 1.9 s | yes |
| `django/django` | 2,954 | 20 | 5.5 s | 5.6 s | yes |
| `encode/httpx` | 62 | 0 | 0.8 s | 0.7 s | yes |
| `fastapi/fastapi` | 1,234 | 42 | 1.4 s | 1.4 s | yes |
| `home-assistant/core` | 20,667 | 549 | 32.8 s | 31.6 s | yes |
| `huggingface/transformers` | 5,209 | 2,221 | 16.4 s | 16.6 s | yes |
| `jakevdp/PythonDataScienceHandbook` | 147 | 2 | 0.6 s | 0.6 s | yes |
| `langchain-ai/langchain` | 2,694 | 92 | 3.5 s | 3.6 s | yes |
| `pallets/flask` | 88 | 1 | 0.8 s | 0.8 s | yes |
| `pandas-dev/pandas` | 1,558 | 5 | 8.8 s | 8.7 s | yes |
| `psf/black` | 456 | 0 | 1.7 s | 1.4 s | yes |
| `psf/requests` | 40 | 0 | 0.6 s | 0.7 s | yes |
| `pydantic/pydantic` | 3,170 | 5 | 2.7 s | 2.7 s | yes |
| `scikit-learn/scikit-learn` | 1,045 | 11 | 3.9 s | 4.0 s | yes |
| `scrapy/scrapy` | 558 | 0 | 1.4 s | 1.4 s | yes |
| `Significant-Gravitas/AutoGPT` | 4,235 | 66 | 10.4 s | 10.7 s | no: 9 fewer |
| `Textualize/rich` | 214 | 1 | 1.2 s | 1.1 s | yes |
| **Total** | **46,747** | **3,056** | **96.3 s** | **95.7 s** | **17 of 18** |

The findings column is for the fixed build. `main` has 3,065: the same ones, and the nine `SLOP051` findings in AutoGPT.

Exit codes were the same on both builds: `1` for 14 projects and `0` for 4. Seven files in two projects could not be parsed, and were reported as such: test data that is not valid Python on purpose (six in `psf/black`, one in `django/django`).

### Findings by rule

Fixed build, offline. An empty cell is 0.

| Project | `SLOP010` | `SLOP011` | `SLOP020` | `SLOP021` | `SLOP022` | `SLOP030` | `SLOP040` | `SLOP051` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `ansible/ansible` | | | | | | 12 | 25 | |
| `celery/celery` | | | | 1 | | 2 | 1 | |
| `django/django` | | | | 7 | | 10 | 3 | |
| `fastapi/fastapi` | | 1 | | | | 41 | | |
| `home-assistant/core` | | 9 | | | | 540 | | |
| `huggingface/transformers` | 3 | | | | | 2,218 | | |
| `jakevdp/PythonDataScienceHandbook` | | | | | | 2 | | |
| `langchain-ai/langchain` | | | | | | 91 | | 1 |
| `pallets/flask` | | | | | | 1 | | |
| `pandas-dev/pandas` | | | | | | | 5 | |
| `pydantic/pydantic` | | 4 | | | | 1 | | |
| `scikit-learn/scikit-learn` | 1 | | | | | 9 | 1 | |
| `Significant-Gravitas/AutoGPT` | 1 | 1 | | 3 | | 33 | 28 | |
| `Textualize/rich` | | | | | | | 1 | |
| **Total** | **5** | **15** | **0** | **11** | **0** | **2,960** | **64** | **1** |

`encode/httpx`, `psf/black`, `psf/requests` and `scrapy/scrapy` have no finding.

97% of all findings are `SLOP030` (duplicate functions), and three quarters of those are in `huggingface/transformers`. That project keeps every model in a file of its own and copies shared helpers into each one on purpose.

### Package rules, with PyPI

Fixed build, `--select SLOP001,SLOP002`. 1,782 different package names were sent to pypi.org. "Time" is with an empty cache.

| Project | Exit | Time | `SLOP001` | `SLOP002` |
|---|---:|---:|---:|---:|
| `home-assistant/core` | 1 | 83.1 s | 0 | 13 |
| `scrapy/scrapy` | 1 | 2.8 s | 0 | 1 |
| `Significant-Gravitas/AutoGPT` | 1 | 8.2 s | 0 | 1 |
| The 15 other projects | 0 | 0.3 s to 6.2 s each | 0 | 0 |

- All 18 projects together took 140.8 s with an empty cache and 38.5 s when run again.
- `SLOP001` reported nothing. No real dependency of these projects was called missing.
- `SLOP002` reported 15 dependencies. Each message is true: the package is under 30 days old, or has one release and is under a year old. All 15 are real dependencies that the projects mean to use, for example 13 small device libraries in Home Assistant's `requirements_all.txt`. A project would list them in `known-packages`.
- PyPI answered every request. No run printed a note about missing answers.
- The cache folder was created with mode `700`, as the cache fix says.

### `--diff` on real history

Each project was checked with `--diff HEAD~25`.

| | Result |
|---|---|
| Projects | 18 |
| Files in the diffs | 1,735 |
| Added and changed lines | 177,734 |
| Places where lines were only removed (new: recorded by the fixed build) | 1,236 |
| Files whose changed lines the two readers disagree on | 0 |
| Findings with `--diff`, `main` | 14 |
| Findings with `--diff`, fixed | 14, the same ones |

The new reader counts the lines of a hunk by the numbers in its header. On real history it gives the same changed lines as the old one. The 1,236 removals caused no finding, which is what #71 asks for: "a deletion that does not create a copy changes nothing".

### Each fix, before and after

Every fix was also reproduced on its own, with the real command line.

| Fix | On `main` | With the fix |
|---|---|---|
| #67: a crash (`tool = 1` in `pyproject.toml`) | Traceback, exit code `1` | `slopfence: internal error (AttributeError). This is a bug in slopfence...`, exit code `2`. `SLOPFENCE_DEBUG=1` adds the traceback |
| #69: `[tool.slopfence]` with `ignore = ["SLOP040"]`, no git | Applied when started in `proj`. Not applied from `proj/src` or from above: 1 issue | "No issues found" from all three folders |
| #70: cache file with mode `600` is rewritten | Mode becomes `664` | Mode stays `600` |
| #71: `sum_prices` becomes a copy of `total_price` by losing a line | `--diff HEAD`: "No issues found in 2 files" | `--diff HEAD`: `SLOP030: 'sum_prices' is identical to 'total_price' in a.py:1` |
| #92: a program named `git` in the project, `PATH` holds `.` | The program runs | It does not run. git from `/usr/bin` is used |
| A repository whose own config sets `core.fsmonitor` to a command | `--diff` runs the command | The command does not run |
| An added line `++ /dev/null` above a placeholder comment | `--diff`: "No issues found" | `--diff` reports `SLOP010` |
| A changed file named `my file.py` | `--diff`: "No issues found" | `--diff` reports its finding |
| `innocent.py`, a link to a file outside the project | The file is read, and its comment is quoted in the report | The link is not read |
| A file name with an escape sequence, or with a line break and `::error ...` | Printed as it is | Written out: `esc\x1b[2J...`, `sub/a\n::error ...` |
| A notebook cell of 4,000 `pip install` lines saved as one string | 2.3 s | 0.09 s |
| `f"\{x}"` above a chat leftover, with `PYTHONWARNINGS=error` | "No issues found" | `SLOP051` is reported |

### The test suite

| Check | Result |
|---|---|
| `python3 .claude/tools/preflight.py` on the fixed build (ruff, format, slopfence on its own code, tests with coverage, docstrings, tool tests, markdownlint) | All checks passed |
| Tests on Python 3.14.4 | 1,869 passed, 1 skipped |
| Tests on Python 3.12.3 | 1,863 passed, 1 skipped |
| Tests on Python 3.10.12 | 1,852 passed, 6 skipped |

Each branch also passed the preflight on its own.

## What the run found

Two bugs showed up in this run that no issue named. Both are fixed, with tests.

**`SLOP051` reported "as an AI SDK".** All nine `SLOP051` findings in `Significant-Gravitas/AutoGPT` were docstrings such as `Emit as an AI SDK v5 data part.` The rule looked for the words "as an AI" anywhere. It now asks for an assistant that speaks of itself: "As an AI language model", or "As an AI, I can't...". After the fix the 18 projects have one `SLOP051` finding left. The standard corpus (22,971 files of the Python standard library and system packages) has no `SLOP051` finding before or after.

**Comments were lost when Python warnings are errors.** `psf/black` has test files with an invalid escape inside an f-string. From Python 3.12 on, reading the comments of such a file raises a `SyntaxWarning`. slopfence printed that warning. With `PYTHONWARNINGS=error`, which many CI runs set, it stopped reading comments at that line: findings in the comments below were missed, and ignore comments below did not count. Comments are now read with warnings switched off.

## Findings read by hand

A sample of five findings per rule was drawn with a fixed seed (2026) from 17 projects, on the build before the `SLOP051` fix. Each one was read in its source file. `home-assistant/core` is not part of the sample.

| Rule | Findings | Read | Right and useful | Right, but on purpose | Wrong |
|---|---:|---:|---:|---:|---:|
| `SLOP010` placeholder | 5 | 5 | 0 | 3 | 2 |
| `SLOP011` stub function | 6 | 5 | 0 | 1 | 4 |
| `SLOP021` trivial assertion | 11 | 5 | 2 | 3 | 0 |
| `SLOP030` duplicate function | 2,420 | 5 | 0 | 5 | 0 |
| `SLOP040` swallowed exception | 64 | 5 | 0 | 5 | 0 |
| `SLOP051` chat leftover | 10 | 5 | 0 | 0 | 5 |
| **Total** | | **30** | **2** | **17** | **11** |

What is behind the numbers:

- **Right and useful (2).** `celery/celery` has `assert 'foo', repr(TaskSet('foo' in None))`: the comma makes the assertion always true, so the test checks nothing. `Significant-Gravitas/AutoGPT` has a test whose only assertion is `assert True`.
- **`SLOP051`, wrong (5).** All five are "as an AI SDK". Fixed in this work.
- **`SLOP011`, wrong (4).** `pydantic` has methods such as `bool_schema`, which returns `{'type': 'boolean'}`. The docstring says "Generates a JSON schema", and a fixed value is the right answer there.
- **`SLOP010`, wrong (2).** "This is a simplified version compared to PostResponse" describes a class. It is no placeholder.
- **`SLOP010`, on purpose (3).** Comments inside docstring examples that speak to the reader, such as `>>> # replace this with your own video file`.
- **`SLOP030`, on purpose (5).** Real identical copies, all in `huggingface/transformers`, which copies helpers into every model file as a rule of the project.
- **`SLOP021`, on purpose (3).** Fixtures of Django's own test runner (`assertEqual(1, 1)`).
- **`SLOP040`, on purpose (5).** Best-effort code, such as a cache write that may fail. The rule has low severity for this reason.

These projects are old and reviewed, so little junk is expected in them. The sample still shows where the rules misread correct code. That is the list in the next section.

## Still open

Found in this work and not fixed. Each one needs an issue and, for a rule, a corpus run of its own.

| What | Detail |
|---|---|
| `SLOP011` reports a function that correctly returns a fixed value | 4 of 5 sampled findings. See `pydantic/json_schema.py` |
| `SLOP030` treats two setters of different global variables as identical | `set_in_sighandler` and `set_default_app` in `celery/celery`. The docstring counts toward the three-statement minimum, and names declared `global` are treated like local names |
| `SLOP010` reports "This is a simplified version" where it describes real code, and reader notes inside docstring examples | All 5 findings in the 18 projects are one of the two |
| `SLOP051` reports model output that a docstring quotes as an example | 1 finding in `langchain-ai/langchain` |
| A `pyproject.toml` with thousands of dependencies is slow | The time grows with the square of the number of entries: 0.9 s for 1,000, 3.3 s for 2,000, 12 s for 4,000. No real project is near that, but a crafted file is |
| The `baseline` key of a config can name any file, also a device | A config from someone else can make the run read a file outside the project |
| `-o` and `--write-baseline` write through a symbolic link | On purpose since #68. A link in a checked project can point the report somewhere else |
| A config in a pull request can switch rules off | `per-file-ignores = {"*" = [...]}` gives exit code `0` and no note. Other linters behave the same |
| The workflows use actions by tag, not by commit | Among them the PyPI publish action |

## What was not tested

- **Windows and macOS.** Only Linux was available. A review of the changes for both systems found one test that would have failed on Windows (it wrote files with Windows line ends), which is fixed. The tests for the git lookup are written to run on Windows in CI, and have not run there yet.
- **Python 3.11 and 3.13.** The suite ran on 3.10, 3.12 and 3.14.
- **git older than 2.15.** The git options were chosen to work there, and were only run with git 2.53.
- **A private package index.** None was configured, so the lookups all went to pypi.org.
- **Code written mostly by an AI assistant.** The 18 projects are mature. A test on fresh, unreviewed AI output would say more about how much junk the rules catch.

## How to repeat it

```bash
git clone --depth 30 https://github.com/psf/requests.git corpus/requests
mkdir run && cd run
python -I -m slopfence --format json --offline ../corpus/requests > offline.json
python -I -m slopfence --format json --select SLOP001,SLOP002 ../corpus/requests > online.json
python -I -m slopfence --format json --offline --diff HEAD~25 ../corpus/requests > diff.json
```

Do the same with the other 17 projects at the commits in [The projects](#the-projects), once with each build, and compare the `findings` lists of the two builds. For one rule on many projects, `.claude/tools/corpus_run.py --rule SLOP051 --sample 30 corpus/*` prints the counts and a sample to read.
