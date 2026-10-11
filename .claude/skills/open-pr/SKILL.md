---
name: open-pr
description: Opens a pull request for the current branch the way this repository expects - checks first, a Conventional Commits title, the filled-in template, the linked issue - and explains what CI and CodeRabbit will require before it can merge. Use when asked to open, create or prepare a pull request.
argument-hint: "[issue-number]"
allowed-tools:
  - Bash(git branch --show-current)
  - Bash(git rev-list *)
  - Bash(git status *)
  - Bash(python3 .claude/tools/preflight.py*)
---

# Open a pull request

Linked issue, if given: `$ARGUMENTS`

## Current state

- Branch: !`git branch --show-current`
- Commits ahead of main: !`git rev-list --count origin/main..HEAD`
- Uncommitted changes: !`git status --short | head -20`

## 1. Before anything is pushed

- If the branch is `main`, stop: create a branch first (`git switch -c feat/<name>`).
- Run `python3 .claude/tools/preflight.py` and fix everything it reports.
- Read your own diff once from top to bottom: `git diff origin/main...HEAD`. Remove debugging leftovers and anything outside the task.
- One pull request does one thing. If the diff does two, split it.

## 2. Title

The title becomes the squash commit on `main`. Use Conventional Commits, in the imperative:

- `feat: add --baseline to hide existing findings`
- `fix: don't flag hooks documented as "override this" (SLOP011)`
- `docs: explain private index detection`
- `perf:`, `refactor:`, `test:`, `ci:`, `chore:` for the rest

## 3. Description

Fill in `.github/PULL_REQUEST_TEMPLATE.md`; keep its headings and checklist.

- **What does this PR do?** What changes for a user and why, then `Closes #<issue>` on its own line (one per issue).
- **How was it tested?** The commands you ran and their results. For a detector: the cases that should and should not be flagged, and the corpus numbers.
- Tick only the boxes that are true.

## 4. Create it

```bash
git push -u origin "$(git branch --show-current)"
gh pr create --title "<title>" --body-file <file-with-the-description>
```

Pushing and creating a pull request are visible to others: do them only when the user asked for a pull request.

## 5. What happens next

- **`ci`** must pass: lint, tests on Python 3.10 to 3.14 plus macOS and Windows, coverage, the package build, and the end-to-end tests of the GitHub Action and pre-commit hook.
- **CodeRabbit** reviews every push. It requests changes while it has open comments or one of its checks fails: a clear title, the linked issue fully addressed, and docstrings on at least 80% of touched functions. Fix what it finds; if a finding is wrong, reply to it with the reason.
- Every review conversation must be resolved, and the branch must be up to date with `main`.
- A maintainer squash-merges. Don't merge it yourself unless the user asks.
