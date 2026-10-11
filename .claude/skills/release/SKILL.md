---
name: release
description: Release checklist for a new slopfence version - tracking issue, release-prep pull request, GitHub release, PyPI upload and the checks after it.
argument-hint: "<version, for example 0.3.0>"
disable-model-invocation: true
---

# Release slopfence $ARGUMENTS

A release publishes to PyPI, and a PyPI version can never be replaced. Do the steps in order, and stop at the first one that fails.

## 1. Scope

- Find or create the tracking issue "Release v$ARGUMENTS" (see the issue for v0.2.0, #25, for the shape).
- Every issue in the `v$ARGUMENTS` milestone is closed or moved to a later milestone.
- `main` is green: `gh run list --branch main --limit 5`.

## 2. Release-prep pull request

On a branch `chore/release-v$ARGUMENTS`, change only these:

- **Version:** `__version__ = "$ARGUMENTS"` in `src/slopfence/__init__.py`. The release workflow refuses a tag that doesn't match it.
- **CHANGELOG.md:**
  - move everything under `[Unreleased]` into `## [$ARGUMENTS] - YYYY-MM-DD`, with upgrade notes first when a default or behaviour changed;
  - leave an empty `[Unreleased]` on top;
  - update the compare links at the bottom.
  - Check every entry against `git log <previous-tag>..HEAD`: nothing missing, nothing that didn't ship.
- **README.md:**
  - the status line (version and number of detectors);
  - the GitHub Action and pre-commit examples (`@v$ARGUMENTS`, `rev: v$ARGUMENTS`);
  - the roadmap: this version marked released, **Later** trimmed.

Title: `chore: release v$ARGUMENTS`. Run `python3 .claude/tools/preflight.py`. CI's `package` job builds and smoke-tests exactly like the release workflow, so a green pull request means the build will work.

## 3. Release

Right after the pull request is merged (the README already points at the new tag, so keep the gap to minutes):

```bash
git switch main && git pull --ff-only
gh release create v$ARGUMENTS --target main --title "v$ARGUMENTS" --notes-file <notes>
```

The notes are the `[$ARGUMENTS]` CHANGELOG section with highlights first, then install and upgrade snippets, upgrade notes, and a link to the full CHANGELOG. See `gh release view v0.2.0` for the shape.

Publishing the GitHub release starts `.github/workflows/release.yml`: it checks the tag against `__version__`, builds, runs `twine check`, smoke-tests the wheel, uploads to PyPI through Trusted Publishing, and attaches the wheel and sdist to the release.

## 4. Check

Wait for the release workflow to finish before installing: until its `publish-pypi` job is done, the version is not on PyPI.

```bash
run_id=$(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$run_id" --exit-status
python -m venv /tmp/release-check && /tmp/release-check/bin/pip install slopfence==$ARGUMENTS
/tmp/release-check/bin/slopfence --version
/tmp/release-check/bin/slopfence --list-rules
```

- <https://pypi.org/project/slopfence/$ARGUMENTS/> shows the version. The "latest" JSON can lag a few minutes behind PyPI's CDN.
- `uses: syedmuhdahmad/slopfence@v$ARGUMENTS` and `rev: v$ARGUMENTS` resolve.

## 5. Close

- Tick the tracking issue's checklist, comment with the links, and close it.
- Close the milestone.

## If something fails

- Before the GitHub release exists: fix it in a new pull request. Nothing is public yet.
- After PyPI has the version: it can't be changed. Fix forward with a patch release. Yank a broken version on PyPI only if installing it does harm.
