# Security Policy

slopfence is partly a **supply-chain security tool** (it detects hallucinated and slopsquatted packages), so we take security reports seriously.

## Supported versions

slopfence is pre-release. Until v1.0, only the latest release and `main` receive security fixes.

| Version | Supported |
|---|---|
| latest release | ✅ |
| `main` | ✅ |
| older releases | ❌ |

## Reporting a vulnerability

**Please do not report security vulnerabilities through public issues, discussions, or pull requests.**

Report privately using GitHub's private vulnerability reporting:

👉 **[Report a vulnerability](https://github.com/syedmuhdahmad/slopfence/security/advisories/new)**

Please include:

- A description of the issue and its impact
- Steps to reproduce, or a proof of concept
- Affected versions or commits
- Any suggested fix, if you have one

## What to expect

| Step | Target time |
|---|---|
| Acknowledgement of your report | within 3 business days |
| Initial assessment | within 7 days |
| Fix or mitigation plan | depends on severity; we'll keep you updated |

We'll coordinate disclosure with you, publish a GitHub Security Advisory once a fix is released, and credit you unless you prefer to stay anonymous.

## Scope

Examples of issues we want to hear about:

- slopfence executing, importing, or installing code from the project it scans
- Registry lookups that can be abused (e.g. leaking private package names to public registries without opt-in)
- Cache poisoning that makes a malicious package appear safe
- A detector bypass that lets a known-malicious package pass as safe
- Vulnerabilities in our GitHub Action or release pipeline

## Checking code you don't trust

slopfence reads the files it checks. It never runs them. Python itself can still import a project file by accident: it looks for modules in the folder a command is started in, and in every folder on `PYTHONPATH`.

- The `slopfence` command, the pre-commit hook and the GitHub Action don't have the checked folder on that path. Use one of them.
- `python -m slopfence` starts with the folder on the path. slopfence removes it before it imports its own modules, but Python has already imported a few of its own by then. Use `python -I -m slopfence` if you need the module form.
- Don't set `PYTHONPATH` to the project when you run slopfence on it. The Action ignores `PYTHONPATH`.

## What slopfence sends over the network

`SLOP001` and `SLOP002` check package names against `https://pypi.org`, with one `GET` request per name, in rare cases two, and never to another address. `SLOP002` reads the answers to `SLOP001`'s lookups and sends no name of its own. By default only names **declared in dependency files** (requirements files, `pyproject.toml`, `setup.cfg`, `setup.py`, `Pipfile`, and `pip install` lines in notebooks) are sent. Import names from source code and notebooks, which can reveal private package names, are sent only when you opt in with `--check-imports` or `check-imports = true`. Dependencies are not sent when they may come from a private index: one set in the dependency file itself, or in pip's or uv's environment variables and configuration files (`pip.conf`, `uv.toml`). For `setup.py`, `setup.cfg`, `Pipfile`, notebooks and requirements files with another name than `requirements*.txt`, a private index in any requirements file, `pyproject.toml`, `Pipfile` or `Pipfile.lock` of the project counts too. So does a `pyproject.toml`, `Pipfile` or `Pipfile.lock` that can't be read: it may set one. A package that the project defines itself is never sent. Neither are the dependencies of a `setup.py`, `setup.cfg` or `Pipfile` under a test folder: such a package is a fixture. Neither is a package that a project file takes from a URL, a path or git, in whichever file it is named. With a private index in the environment, nothing is sent at all. Index URLs are never printed. See [Private package indexes](README.md#private-package-indexes) for the full list. `known-packages` are never sent. Neither are the names in a file where `SLOP001` is switched off with `per-file-ignores`, or under a `# slopfence: ignore` or `# slopfence: ignore-file` comment. `--offline` sends nothing. Ignoring `SLOP001` also sends nothing, unless you select `SLOP002` by name. See [What slopfence sends over the network](README.md#what-slopfence-sends-over-the-network).

If slopfence sends anything beyond this, please report it as a vulnerability.

Out of scope: detectors that miss a code-quality pattern (please open a normal issue instead).
