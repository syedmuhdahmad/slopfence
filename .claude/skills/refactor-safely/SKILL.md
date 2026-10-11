---
name: refactor-safely
description: Procedure for changing slopfence's structure without changing its behaviour - small steps, with the findings on a real-code corpus as the safety net. Use when asked to refactor, restructure, deduplicate, split a module, or speed up code where results must stay the same.
argument-hint: "<what to refactor>"
---

# Refactor without changing behaviour

Target: `$ARGUMENTS`

A refactoring in a linter is safe when every finding on a large body of code is the same before and after: same rule, same file, same line, same message, same order.

## 1. Pin the current behaviour

On the commit you start from, save what slopfence reports today:

```bash
.venv/bin/python -m slopfence /usr/lib/python3.14 --offline --format json --exit-zero -o /tmp/before.json
```

Use any large folder of real Python; the standard library of your interpreter is always there (`python3 -c "import sysconfig; print(sysconfig.get_paths()['stdlib'])"`). Also note that the test suite is green before you touch anything.

## 2. Say what will change, and what won't

- One sentence for the goal ("`engine.run` no longer builds the package index itself").
- The public surface that must not move: CLI options, exit codes, report formats, `engine.run`'s signature, the `Registry` protocol, the names tests import.
- If the plan needs more than one sentence per step, split it into smaller steps.

## 3. Move in small steps

- One kind of change per step: extract a function, move it, rename it, inline it. Never mix a move with a behaviour change.
- After each step: `python3 .claude/tools/preflight.py --quick`.
- Search before you extract: the helper may already exist (see `.claude/rules/python.md`).
- Update callers in the same step. No compatibility shims and no dead code left behind "for now".
- Don't rewrite tests to fit the new structure unless they test a private name that moved. A test that must change is a sign that behaviour changed.

## 4. Prove nothing changed

```bash
.venv/bin/python -m slopfence /usr/lib/python3.14 --offline --format json --exit-zero -o /tmp/after.json
diff /tmp/before.json /tmp/after.json && echo "findings identical"
```

Any difference is a behaviour change. Either undo it, or take it out of this refactoring and treat it as its own change with its own issue.

For a performance change, also time both versions on the same corpus, three runs each, and report the numbers.

## 5. Finish

- `python3 .claude/tools/preflight.py` passes in full.
- Ask the `architecture-reviewer` agent to review the diff with a fresh eye.
- The pull request title starts with `refactor:` (or `perf:`), and the description says the findings on the corpus are identical, with the corpus size. No CHANGELOG entry unless users can notice the change.
