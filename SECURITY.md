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

## What slopfence sends over the network

`SLOP001` checks package names against `https://pypi.org`. By default only names **declared in dependency files** (`requirements*.txt`, `pyproject.toml`) are sent. Import names from source code, which can reveal private package names, are sent only when you opt in with `--check-imports` or `check-imports = true`. Dependencies are not sent when they may come from a private index: one set in the dependency file itself, in a Pipfile, or in pip's or uv's environment variables and configuration files (`pip.conf`, `uv.toml`). With a private index in the environment, nothing is sent at all. Index URLs are never printed. See [Private package indexes](README.md#private-package-indexes) for the full list. `known-packages` are never sent. Neither are the names in a file where `SLOP001` is switched off with `per-file-ignores`, or under a `# slopfence: ignore` or `# slopfence: ignore-file` comment. `--offline` sends nothing. See [What slopfence sends over the network](README.md#what-slopfence-sends-over-the-network).

If slopfence sends anything beyond this, please report it as a vulnerability.

Out of scope: detectors that miss a code-quality pattern (please open a normal issue instead).
