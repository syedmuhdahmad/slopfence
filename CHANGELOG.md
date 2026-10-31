# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `SLOP001`: an ignore comment on a `pyproject.toml` requirement written with TOML escapes, such as `"corp-lib; python_version<\"3.12\""`, now stops both the finding and the PyPI lookup. Before, the name was sent to pypi.org and reported on the line that opens the array. The same happened to an entry after one that holds an escaped `]`, and to an entry that a comment on the array's first line names. Each entry is now reported on its own line. A comment on the line that opens the array still covers the entries that v0.3 reported there ([#97](https://github.com/syedmuhdahmad/slopfence/issues/97))
- In `pyproject.toml` and `Pipfile`, ignore comments after a multi-line string that holds `\"""`, or that has a quote just before its closing `"""`, are no longer missed. Such a string now ends where TOML ends it. A `# slopfence:` directive in its text no longer counts, as in any other string ([#97](https://github.com/syedmuhdahmad/slopfence/issues/97))

## [0.3.0] - 2026-10-14

A new rule for packages that are new on PyPI, a baseline for the findings you already have, more dependency files, and worker processes for large projects.

### Upgrade notes

- On projects with 200 or more Python files, slopfence now starts worker processes: one for every 100 files, up to one per CPU and at most 8. Findings are the same. To switch this off, pass `--jobs 1` or set `jobs = 1` in `[tool.slopfence]` ([#12](https://github.com/syedmuhdahmad/slopfence/issues/12)).
- **Starting on a project that already has findings is easier.** Record them once with `--write-baseline`, then fail only on new ones with `--baseline`. See "Baseline: start with the code you have" in the README ([#51](https://github.com/syedmuhdahmad/slopfence/issues/51)).
- `--list-rules` now reads `[tool.slopfence]`. With a config that is not valid it exits with code `2`, where v0.2 printed the rules ([#52](https://github.com/syedmuhdahmad/slopfence/issues/52)).
- JSON reports have a new `baselined` key. It is `0` when no baseline is used ([#51](https://github.com/syedmuhdahmad/slopfence/issues/51)).
- **`SLOP001` reads more files:** `setup.cfg`, `setup.py`, `Pipfile`, Jupyter notebooks and more requirements file names. The package names declared there are now sent to pypi.org, like those in `requirements.txt`. They are not sent when a requirements file, `pyproject.toml`, `Pipfile` or `Pipfile.lock` of the project uses a private index. To leave a kind of file out, add it to `exclude`, for example `exclude = ["*.ipynb"]` ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53)).
- `Pipfile` entries are now checked. Before, the file was only read with `--check-imports` ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53)).
- **`SLOP002` is on by default.** A dependency that has been on PyPI for less than 30 days, or that has a single release and is less than a year old, is now reported with high severity. If it is the package you mean, add `# slopfence: ignore[SLOP002]` to its line. List your own new packages in `known-packages` ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39)).
- slopfence itself was first uploaded to PyPI on 2026-10-02. Until the end of October 2026, `SLOP002` reports `slopfence` in your requirements files, like any other package that new. No name is exempt ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39)).
- **PyPI lookups are now `GET https://pypi.org/simple/<name>/`** (the first 16 KB) instead of `HEAD https://pypi.org/pypi/<name>/json`. The same names are sent as before. If you ignore `SLOP001`, still nothing is sent: `SLOP002` then does not run, and the report says so in a note. To run it alone, name it in `select` ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39)).
- Packages that are already in your cache as "exists" are checked by `SLOP002` when their entry expires, within a week. Delete `~/.cache/slopfence/pypi.json` to check everything now ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39)).
- Upgrade the pre-commit hook and the GitHub Action together with the command line. A v0.2 run that shares the cache stores answers without upload dates, and `SLOP002` says nothing about those packages for up to a week ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39)).
- A project that had a `SLOP001` finding in its baseline for a package that has since been registered on PyPI may now see a `SLOP002` finding for it. That is the case the rule is for ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39)).
- An index on `test.pypi.org` now counts as a private index: TestPyPI holds other packages than PyPI. An index is public when its host is `pypi.org`, `files.pythonhosted.org` or `pypi.python.org` ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53)).

### Added

- **`SLOP002` slopsquatting risk:** a dependency that exists on PyPI is reported when its oldest file there is less than 30 days old, when it is less than a year old and has a single release, or when PyPI has quarantined it. With `--check-imports` it also covers imports that are not installed or declared. The message names the upload date and says what to check. Nothing is reported for a package that is a year old or more, for one that is not looked up, or when PyPI gives no answer. A name that looks like a popular package is never a reason. `SLOP002` sends no name that `SLOP001` would not send ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39))
- `--min-package-age DAYS` option and `min-package-age` config key: how many days a package must have been on PyPI before `SLOP002` stops reporting it as new (1 to 365, default 30). The single-release check always looks back one year ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39))
- `# slopfence: ignore[SLOP002]` on a dependency's line silences `SLOP002` and keeps `SLOP001`, in every kind of dependency file and in notebooks ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39))
- `--jobs N` option and `jobs` config key: files are read, parsed and checked in up to `N` worker processes. Default: one per CPU, at most 8. The report is the same with any number of jobs. If worker processes can't start, a warning is printed and the files are checked in one process ([#12](https://github.com/syedmuhdahmad/slopfence/issues/12))
- **Baseline mode:** `--write-baseline FILE` records the findings of a run. `--baseline FILE`, or `baseline = "FILE"` in `[tool.slopfence]`, hides them, so only new findings are reported and only new findings fail the run. `--no-baseline` shows everything again. A finding is matched by its file, its rule and what it is about (a package, function or test name, or the text of the flagged comment), not by its line number. So it stays hidden when code moves or is reformatted. It comes back when the function, the test or the file is renamed, or the comment is reworded. It works with `--diff`, `--fail-on` and every output format ([#51](https://github.com/syedmuhdahmad/slopfence/issues/51))
- With a baseline, the summary says how many findings were hidden, JSON reports have a `baselined` count, and SARIF reports leave baselined findings out and say how many there were. A note says when baselined findings are gone, so the file can be written again. It only comes when the run checks everything the baseline holds ([#51](https://github.com/syedmuhdahmad/slopfence/issues/51))
- `--write-baseline` is careful with the file it replaces. It stops before the scan when the file is not a baseline (a forgotten file name can't destroy source code), and when the file holds findings that the run does not check: for files outside the checked paths, for excluded files, or for rules that are switched off. The file is replaced in one step. A note says when `SLOP001` findings may be missing because PyPI was not asked or did not answer ([#51](https://github.com/syedmuhdahmad/slopfence/issues/51))
- **Per-file ignores:** `[tool.slopfence.per-file-ignores]` turns rules off in the files that match a pattern. A pattern works like an `exclude` pattern. Dependencies and imports in a file where `SLOP001` is off are not sent to PyPI ([#52](https://github.com/syedmuhdahmad/slopfence/issues/52))
- **Severity overrides:** `[tool.slopfence.severity]` gives a rule another severity. Text, JSON and SARIF reports, `--fail-on` and `--list-rules` use it ([#52](https://github.com/syedmuhdahmad/slopfence/issues/52))
- `SLOP001` also checks ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53)):
  - `setup.cfg`: `install_requires`, `setup_requires`, `tests_require` and `[options.extras_require]`
  - `setup.py`: the same keywords of `setup()`, when the list is written in the file or in a variable. The file is read with `ast` and never run
  - `Pipfile`: `[packages]` and `[dev-packages]`
  - Not a `setup.py`, `setup.cfg` or `Pipfile` under a folder named `tests` or `test`: such a package is a fixture, and its made-up dependencies are not looked up. Its name and its private index still count
  - Jupyter notebooks: `pip install` lines in code cells and, with `--check-imports`, imports. A finding has the line and column of the `.ipynb` file, so `--diff` and SARIF work. A notebook that sets a private index, in a `pip install` line or anywhere else in a code cell, is not looked up. The imports of a notebook are only looked up when it installs everything it uses by name. They are not looked up when one of its `pip install` lines installs something without a name (a path, a URL, `-r requirements.txt`), when it installs with `conda install`, `mamba install`, `uv add`, `poetry add`, `pipenv install` or `python setup.py install`, when it fetches code (`git clone`, `wget`, `curl`, `gdown`, `unzip`, `tar`, `7z`, `cp -r`, or `cp`, `mv` or `ln` with a `.py` file), or when it changes where Python looks for modules (`%cd` with or without the `%`, `os.chdir`, `sys.path`, `PYTHONPATH`). A module that a `%%writefile` cell writes is the notebook's own. A notebook larger than 20 MB is not read
  - More requirements file names: `dev-requirements.txt`, `requirements.in`, `constraints.txt`, and `.txt` / `.in` files inside a `requirements/` folder
  - A file with one of these new names that is a plain list of names (no version, extra, marker or option anywhere) may be a list for apt or R. Nothing in it is reported when PyPI lacks more of its names than it has
- A private index in a project file also covers the files that don't name one. When a requirements file, `pyproject.toml`, `Pipfile` or `Pipfile.lock` uses a private index, the dependencies in `setup.py`, `setup.cfg`, `Pipfile`, notebooks and requirements files with another name than `requirements*.txt` are not looked up, and a note says so. A `pyproject.toml`, `Pipfile` or `Pipfile.lock` that can't be read (broken, written in a newer TOML than Python reads, or not UTF-8) counts like one with a private index: it may set one that can't be seen. An index in an excluded file doesn't count, and `detect-private-index = false` switches this off ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53))
- A file or folder that is named on the command line in a folder slopfence does not walk (a nested checkout, `build`, a virtual environment), or outside the project, is checked together with the dependency and lock files of its own checkout. A file that has none is checked together with the files that lie directly in its own folder. So a private index that is set there covers it ([#64](https://github.com/syedmuhdahmad/slopfence/issues/64))
- `dependency_links` in `setup.py` or `setup.cfg` make that file's packages private. So do `find_links`, and an `index_url` that is not PyPI, in the `[easy_install]` section of `setup.cfg`. They also cover the `setup.py` or `setup.cfg` in the same folder, because setuptools reads the two as one ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53))
- With `--check-imports`, unknown imports are not looked up either when a notebook sets a private index, or when a `setup.py` or `setup.cfg` names another source in one of these ways: they may be private packages ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53))
- A package that the project defines itself is never looked up. Its name comes from `[project] name` or `[tool.poetry] name` in any `pyproject.toml`, `[metadata] name` in any `setup.cfg`, and `setup(name=...)` in any `setup.py` of the project ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53))

### Changed

- One PyPI lookup per package now answers both package rules. The cache file keeps its `{"name": [found, time]}` entries and gains a `_meta` entry with the upload facts. v0.2 can still read it, and a cache that pins answers the v0.2 way still answers without a request ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39))
- A missing package, or one that is under a year old or quarantined, is asked about again after a day. An established one is asked about again after a week, as before ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39))
- A rate limit (`429`), an outage, a redirect, an answer that breaks off or an answer that is not PyPI's (a Wi-Fi login page, a proxy page) ends the lookups for that run and is not cached. v0.2 kept asking, and cached such a page as "exists" for a week ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39))
- A body that PyPI sends too slowly is given up after 10 seconds, and counts as no answer ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39))
- `known-packages` is also the place for your own new public packages: a known package is never looked up, so neither rule reports it ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39))
- The notes about lookups that were not made name the package rules that ran: `SLOP001 and SLOP002 made no PyPI lookups: ...` ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39))
- Requirements files are read more strictly. A line that pip would reject gives no finding, `python>=3.9` is not a package, and neither is a file name such as `vendor.tar.gz` or a name with a bracket that is no list of extras (bindep's `libffi-dev [platform:dpkg]`). The requirement strings in `pyproject.toml` are read the same way. In these two kinds of file, the name that such a line starts with still counts as declared, so `--check-imports` does not look up the import of that package. A document that is only named like a requirements file is not looked up: a file with more prose than requirements, or with an underline (`-----`, `=====`), a bullet (`- 4 GB RAM`, `* 4 GB RAM`), a directive (`.. toctree::`), a quote or a table row. A package that such a file takes from a URL, a path or git still counts as coming from there ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53))
- The packages in a requirements file for another package manager (`requirements-conda.txt`, `requirements.apt.txt`) are no longer checked: they are not PyPI names. A private index that such a file names still counts ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53))
- A package that one project file takes from a URL, a path, git or an index it names (`corp-auth @ git+https://...`, a `[tool.uv.sources]` entry, a Poetry `source`) is no longer looked up in the other files that name it. Before, `corp-auth` in `pyproject.toml` was sent to PyPI and reported although `requirements.txt` installed it from git. In `pyproject.toml`, a reference to a path counts as well (`corp-auth @ file:../corp-auth`, Hatch's `corp-auth @ {root:uri}/corp-auth`): v0.2 looked such a package up ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53))
- A requirements file that is not UTF-8 (UTF-16 as Windows PowerShell writes it, or the encoding of the locale) is still not looked up. A private index that it names now counts. So does an entry that it takes from a URL, a path or git: that package is not looked up in another file either ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53))
- `--find-links` and `--no-index` in a requirements file make that file's packages private: a name that PyPI doesn't have may come from the links ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53))
- The pre-commit hook also runs when a notebook, a `setup.cfg`, a `Pipfile`, a requirements file that ends in `.in`, or a constraints file changes. Before, it ran for Python files, `pyproject.toml`, and `.txt` files with `requirements` in their path ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53))
- With `--diff`, dependency files outside the diff are no longer looked up on PyPI. Their findings were already left out ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53))
- **Much faster on big projects, with far less memory.** The CPython standard library and 127 installed packages (22,864 files) took 5 min 44 s and 8 GB of memory in v0.2.0. Now they take a little over 2 minutes with one process, and 35 to 45 seconds with 8 jobs on 4 cores. The main process needs about 0.3 GB, and each worker 40 to 80 MB ([#12](https://github.com/syedmuhdahmad/slopfence/issues/12))
- Checking a few files of a big project (pre-commit) is faster. The rest of the project is only read for what `SLOP030` needs, and its test files are not read at all ([#12](https://github.com/syedmuhdahmad/slopfence/issues/12))
- PyPI lookups are sent 8 at a time instead of one by one. 200 uncached names took more than 90 seconds and now take about 10. What is sent has not changed. A name is requested once per run, also when PyPI gives no answer for it ([#54](https://github.com/syedmuhdahmad/slopfence/issues/54))
- The PyPI cache file is written in one step, so two runs at the same time can't leave a broken file ([#54](https://github.com/syedmuhdahmad/slopfence/issues/54))
- Ctrl-C during a scan prints `slopfence: interrupted` and exits with status `130`, instead of a traceback. On Windows, worker processes that are still starting can print tracebacks of their own ([#12](https://github.com/syedmuhdahmad/slopfence/issues/12))
- With `--check-imports`, an import under an ignore comment (`# slopfence: ignore` on its line, or `# slopfence: ignore-file`) is no longer looked up on PyPI. Its finding was already hidden. Dependencies with such a comment were never looked up ([#52](https://github.com/syedmuhdahmad/slopfence/issues/52))
- `--list-rules` reads the project's config and shows the severities the project uses. A rule with another severity than its own also shows `(default: ...)`. Without overrides the output is the same as before ([#52](https://github.com/syedmuhdahmad/slopfence/issues/52))
- SARIF: a rule's `defaultConfiguration.level` is the severity the run used. It only differs from before when `[tool.slopfence.severity]` changes the rule ([#52](https://github.com/syedmuhdahmad/slopfence/issues/52))

### Fixed

- A file with a very long expression or statement chain stopped the whole run with a `RecursionError` traceback and exit code `1`: an `if` with about 1,000 `elif` branches, a condition or a return type with about 1,000 terms, or about 1,000 chained attributes. Generated code often looks like this. Such files are now checked like any other. A file that Python's own parser refuses, because it is nested too deeply for it, is still skipped with the usual "could not parse" warning ([#66](https://github.com/syedmuhdahmad/slopfence/issues/66))
- SARIF: every rule's `helpUri` pointed to `#planned-detectors`, a README section that no longer exists, so GitHub code scanning opened the top of the page. It now points to the detector table, and a test keeps the link and the README heading in step ([#80](https://github.com/syedmuhdahmad/slopfence/issues/80))
- `--diff` found no changed lines when the git config sets `diff.mnemonicPrefix` or `diff.dstPrefix`, so every finding was dropped and the run passed. With `diff.noprefix`, files in a folder named `b` were missed. slopfence now tells git which prefixes to write ([#65](https://github.com/syedmuhdahmad/slopfence/issues/65))
- With `--check-imports`, an import whose name PyPI can't hold as a package name (`import café`, `import _private`) ended the run with a traceback, or was sent to PyPI and reported. Such a name is no longer sent, and it is no longer reported: PyPI is not asked about it, and only PyPI's own answer says that a package is missing ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39))
- With `--check-imports`, an import of the project's own package was looked up, and reported, when the package has no Python source in the project (a Rust or C extension) and is not installed where slopfence runs. A name that a `pyproject.toml`, `setup.cfg` or `setup.py` of the project gives it is now enough ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39))
- An ignore comment at the end of a requirement that goes on over several lines (a pin with `--hash` lines, as `pip-compile --generate-hashes` writes it) was not read, and pip allows a comment nowhere else in such a requirement. It now counts for the requirement ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39))
- An answer to a lookup that is not HTTP at all ended the run with a traceback. It now counts as no answer ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39))
- A damaged cache file (an entry that is not `[found, time]`, or a time of hundreds of digits) could end the run with a traceback. Such an entry is now looked up again ([#39](https://github.com/syedmuhdahmad/slopfence/issues/39))
- A checkout nested inside the project (a git worktree, clone or submodule, such as Claude Code's `.claude/worktrees/`) no longer counts as project code. Before, `SLOP030` reported every function as a duplicate of its copy, and `SLOP001` treated its modules as the project's own. A nested checkout that you name on the command line is still checked ([#58](https://github.com/syedmuhdahmad/slopfence/issues/58))
- A `pyproject.toml` whose tables have another shape than the format asks for (`project = "demo"`) stopped the run with a traceback. It is now read as far as it goes. A `pyproject.toml`, `Pipfile` or lock file that the TOML or JSON reader gives up on (thousands of nested brackets, a number of ten thousand digits) is skipped ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53))
- An index option in a requirements file was only seen at the start of a line, written in full and with a space before its value. It is now also seen after another option (`--pre --extra-index-url ...`), after a requirement (`corp-auth==1.0 --extra-index-url ...`), with its value attached (`-iURL`), and cut short the way pip allows (`--extra-index`). The same goes for `--find-links` and `--no-index`. Before, the packages of such a file were sent to PyPI ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53))
- A form feed in a requirements file, and a U+0085 or U+2028 character in a requirements file or a `pyproject.toml`, counted as a line break. Every finding below it was reported one line too low, so `--diff` could drop it. In `pyproject.toml`, an ignore comment below it covered the entry on the line above instead of its own. Lines are now counted as git and editors count them. pip ends a line at such a character, so what follows it on the same line is still read as a line of its own: a requirement, an index option, an entry from git ([#64](https://github.com/syedmuhdahmad/slopfence/issues/64))
- A Poetry dependency written as a table of its own (`[tool.poetry.dependencies.name]`) or with a dotted key (`name.version = "1.0"`) was reported on the line of `[tool.poetry.dependencies]`, and no ignore comment on the entry covered it. It is now reported on its own line, and an ignore comment there covers it. A comment on the line where it was reported before still covers it too. `[packages.name]` in a `Pipfile` is reported on its own line as well ([#64](https://github.com/syedmuhdahmad/slopfence/issues/64))
- With `--check-imports`, a standard library module that the running Python does not have was sent to PyPI and reported as a missing package: `imp` or `asyncore` on Python 3.12 and later, `cgi` on Python 3.13 and later, `tomllib` on Python 3.10. A module that any Python 3 has or had is now never looked up, so the same file gets the same report on every Python ([#64](https://github.com/syedmuhdahmad/slopfence/issues/64))
- A Python file with an expression nested more deeply than Python's parser takes (thousands of operators or brackets, in a generated file) stopped the run with a traceback. It is now a file that could not be parsed, like any other file that is not valid Python: a warning, or an error with `--strict` ([#64](https://github.com/syedmuhdahmad/slopfence/issues/64))
- A quoted key in a Poetry dependency table that is not a package name (`"my pkg" = "*"`) stopped the run with a traceback. Such a key is no longer looked up, in a Poetry table or in a `Pipfile` ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53))
- `--diff` in a repository where a file type has a `textconv` diff driver (some notebook tools set one for `.ipynb` files): git counted the lines of the converted text, so a finding on a changed line could be left out. The changed lines are now taken from the files as they are ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53))
- A `pyproject.toml` with a line that starts with `[` and goes on with a long run of spaces took very long to read: over 2 seconds for 1,500 spaces in v0.2, and eight times longer for twice as many. It is now read in the time it takes to read the line once ([#53](https://github.com/syedmuhdahmad/slopfence/issues/53))
- Findings at the same position (`import a, b` where neither exists) could be listed in a different order from run to run. They are now ordered by message ([#12](https://github.com/syedmuhdahmad/slopfence/issues/12))
- `SLOP030`: checking a folder or a few files now reports the same duplicates in them as checking the whole project. Before, one copy could be missed when the first copy was outside the checked paths, or when folder names sort differently as folders and as text (`a/` and `a-b/`) ([#52](https://github.com/syedmuhdahmad/slopfence/issues/52))
- A file that Python only warns about (an invalid escape sequence such as `"\d"`) counted as unparseable when warnings were turned into errors (`python -W error`). Such a file is now always parsed, and Python's warning about it is no longer printed ([#12](https://github.com/syedmuhdahmad/slopfence/issues/12))

### Security

- **The GitHub Action no longer imports files of the repository it checks.** Python puts the folder a command is started in first on its import path. A file there that is named like a standard module (`random.py`, `argparse.py`) was imported in its place, and so run. The Action now starts every Python (`venv`, `pip` and slopfence) in isolated mode (`-I`) and installs slopfence from outside the repository. Every release up to v0.2.0 is affected.
- Isolated mode also ignores `PYTHONPATH`. In the Action, with `--check-imports`, a package that slopfence only found through `PYTHONPATH` no longer counts as installed, so its import can be looked up on PyPI. List such packages in `known-packages`.
- **`python -m slopfence` takes the current folder off its import path** before it imports its own modules. A project with a `random.py` no longer breaks the run or gets that file run. This is not complete: to start a module, Python itself first imports a few of its own (`importlib`, and on older versions `types` and others) and the `slopfence` package, with the folder still on the path. For code you don't trust, use the `slopfence` command or `python -I -m slopfence`. The `slopfence` command and the pre-commit hook never had the folder on their path, unless `PYTHONPATH` names it.
- Worker processes are new in this release. They are only started where a new Python process can be kept from importing project files: on Python 3.11 or newer that was not started with `-E`, on any Python started with `-I` or `-P`, or where workers are forked (Linux before Python 3.14). Elsewhere, for example on Python 3.10 on macOS and Windows, the files are checked in one process and a warning says so. `--jobs 1` avoids the warning ([#12](https://github.com/syedmuhdahmad/slopfence/issues/12)).

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

[Unreleased]: https://github.com/syedmuhdahmad/slopfence/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/syedmuhdahmad/slopfence/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/syedmuhdahmad/slopfence/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/syedmuhdahmad/slopfence/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/syedmuhdahmad/slopfence/releases/tag/v0.1.0
