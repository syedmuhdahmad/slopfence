---
paths:
  - "src/slopfence/registry.py"
  - "src/slopfence/listing.py"
  - "src/slopfence/indexes.py"
  - "src/slopfence/detectors/packages.py"
  - "src/slopfence/detectors/dependency.py"
  - "src/slopfence/detectors/setuptools_files.py"
  - "src/slopfence/detectors/pip_commands.py"
  - "src/slopfence/detectors/notebooks.py"
  - "src/slopfence/detectors/import_names.py"
---

# Network and privacy rules

The README section "What slopfence sends over the network" is a promise to users. Code in these files must keep it.

- slopfence talks to `pypi.org` only. A new host needs an agreed issue and a README change first.
- **An unknown answer is never a finding.** A timeout, a refused connection, a rate limit or an unexpected status means "don't know", and "don't know" is silent.
- Nothing is sent for: names in `known-packages`, dependencies from a file that uses a private index, any run with `--offline`, or any run where the environment configures a private index.
- Nothing is sent for a name the project explains itself, in any file: one of its own packages (`ProjectIndex.own_names`), or a package that some file takes from git, a URL, a path or a named index (`ProjectIndex.sourced_names`).
- **A newer version never sends a name an older one kept on the machine.** A dependency file that v0.2 did not read (`setup.py`, `setup.cfg`, `Pipfile`, a notebook, a requirements file with a new name) inherits a private index from any other dependency file of the project. A new place to read names from must do the same, and needs a test with a registry that records what was asked.
- Import names from source code are sent only with `--check-imports`.
- Send package names and nothing else: no code, no file names, no telemetry.
- **Never print, log or put in a message an index URL.** It can contain credentials. Say where the index is configured instead (`PIP_INDEX_URL`, `pip.conf`).
- An index is public only when its host is `pypi.org`, `files.pythonhosted.org` or `pypi.python.org` (`indexes.is_public_index`). Compare whole hosts, never substrings: TestPyPI is not public.
- The cache file (`~/.cache/slopfence/pypi.json`) must stay readable by older and newer versions: CI pins answers in it as `{"name": [found, timestamp]}`. The facts `SLOP002` uses are kept apart, under the key `_meta`, with a timestamp of their own. An entry without facts is trusted as it is: no request, and no `SLOP002` finding.
- Tests use `FakeRegistry` or a pinned cache. A test that opens a socket is a bug.
