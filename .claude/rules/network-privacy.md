---
paths:
  - "src/slopfence/registry.py"
  - "src/slopfence/indexes.py"
  - "src/slopfence/detectors/packages.py"
  - "src/slopfence/detectors/import_names.py"
---

# Network and privacy rules

The README section "What slopfence sends over the network" is a promise to users. Code in these files must keep it.

- slopfence talks to `pypi.org` only. A new host needs an agreed issue and a README change first.
- **An unknown answer is never a finding.** A timeout, a refused connection, a rate limit or an unexpected status means "don't know", and "don't know" is silent.
- Nothing is sent for: names in `known-packages`, dependencies from a file that uses a private index, any run with `--offline`, or any run where the environment configures a private index.
- Import names from source code are sent only with `--check-imports`.
- Send package names and nothing else: no code, no file names, no telemetry.
- **Never print, log or put in a message an index URL.** It can contain credentials. Say where the index is configured instead (`PIP_INDEX_URL`, `pip.conf`).
- An index is public only when its host is `pypi.org` or `pythonhosted.org`. Compare hosts, never substrings.
- The cache file (`~/.cache/slopfence/pypi.json`) must stay readable by older and newer versions: CI pins answers in it as `{"name": [found, timestamp]}`.
- Tests use `FakeRegistry` or a pinned cache. A test that opens a socket is a bug.
