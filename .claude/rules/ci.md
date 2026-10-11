---
paths:
  - ".github/**"
  - "action.yml"
  - ".pre-commit-hooks.yaml"
---

# CI, GitHub Action and pre-commit hook rules

- `ci` is the one required status check. A new job must be listed in its `needs`, or a failure in it won't block merging.
- Give each job the least `permissions` it needs. Jobs must still work for pull requests from forks, whose token is read-only.
- **Never put untrusted input in a `run:` script with `${{ }}`**, quoted or not: pull request titles, branch names, labels, commit messages. Pass it through `env:` and use the quoted variable.
- The composite action (`action.yml`) must work on Linux, macOS and Windows runners, and must not install into the runner's system Python.
- End-to-end tests pin PyPI answers in the slopfence cache, so results don't depend on the live registry. Keep that when you add a test.
- `.github/action-fixture/` is sloppy on purpose. Its findings are asserted in `.github/workflows/action.yml`; change both together or neither.
- After changing `.pre-commit-hooks.yaml`, run `.github/scripts/test-pre-commit-hook.sh .` (needs `pre-commit`).
- The `package` job builds and smoke-tests exactly like `release.yml`. Keep the two in step.
