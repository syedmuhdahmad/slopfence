# Claude Code setup for slopfence

This folder configures [Claude Code](https://code.claude.com/docs) for this repository. Nothing here ships in the package. It follows Claude Code's [best practices](https://code.claude.com/docs/en/best-practices): a short `CLAUDE.md`, rules that load only where they apply, hooks for what must always happen, and specialists for work that needs a fresh pair of eyes.

Start a new session after pulling changes to this folder, so Claude Code loads them.

## What loads when

| Piece | Where | When Claude sees it |
|---|---|---|
| Project instructions | [`../CLAUDE.md`](../CLAUDE.md) | Every session |
| Coding standards | [`rules/`](rules) | When Claude reads or edits a matching file |
| Skills | [`skills/`](skills) | When you type `/name`, or when the task matches |
| Agents | [`agents/`](agents) | When Claude delegates to one, or you ask for it by name |
| Hooks | [`settings.json`](settings.json), [`tools/`](tools) | Always: Claude Code runs them, not Claude |
| Workflow | [`workflows/`](workflows) | When you type `/review-change` |
| Output style | [`output-styles/`](output-styles) | After you select it |

## Rules

| File | Applies to | About |
|---|---|---|
| `python.md` | `src/`, `tests/`, tool scripts | Design (one job per unit, extend instead of edit, depend on protocols), reuse before writing, style |
| `detectors.md` | `src/slopfence/detectors/`, `models.py` | Low false positives, tests both ways, corpus runs, the new-rule checklist |
| `network-privacy.md` | `registry.py`, `indexes.py`, package checks | What may leave the machine, and what an unreachable registry means |
| `tests.md` | `tests/` | Offline tests, fixtures, cross-platform, coverage |
| `docs.md` | README, CHANGELOG and other docs | Plain style, documenting only what exists |
| `ci.md` | `.github/`, `action.yml`, the pre-commit hook | Permissions, untrusted input, fork pull requests |

## Skills

| Command | What it does |
|---|---|
| `/preflight [--quick]` | Runs everything CI gates on and reports pass or fail per check |
| `/corpus-run <RULE> [paths]` | Runs a rule over real-world code and prints a random sample to classify |
| `/add-detector <issue>` | The procedure for a new rule, from the agreed issue to the pull request |
| `/fix-issue <issue>` | Reproduce a bug or false positive as a failing test, fix the cause, check nothing else moved |
| `/refactor-safely <target>` | Restructure in small steps, with identical findings on a corpus as proof |
| `/open-pr [issue]` | Checks, title, template and what CI and CodeRabbit will ask for |
| `/release <version>` | The release checklist. Only runs when you type it |

## Agents

Each agent has one narrow job and its own context, so its reading does not fill yours. All but `docs-editor` are read-only.

| Agent | Ask it to |
|---|---|
| `false-positive-auditor` | Measure how often a rule is wrong on a corpus, from a random sample |
| `detector-reviewer` | Try to make a detector flag ordinary, correct code |
| `architecture-reviewer` | Find duplication, mixed responsibilities and interfaces that grew for one caller |
| `portability-reviewer` | Find what breaks on Python 3.10, Windows or macOS |
| `privacy-reviewer` | Check a change against the README's promise about network use |
| `docs-editor` | Bring README and CHANGELOG in line with a change, in plain words |

Example: `use the detector-reviewer agent on my changes`.

`/review-change` runs the four reviewers on the current branch at once, then has a fresh agent try to refute each finding, and returns only the confirmed ones. It uses many more tokens than a single review.

## Hooks

Instructions can be forgotten. These can't.

| When | Script | What it guarantees |
|---|---|---|
| Session start | `hook_session_context.py` | Claude knows the branch, and that `main` is protected |
| Before a shell command | `hook_guard_git.py` | No commit on `main`, no push to `main`, in this repository |
| Before an edit | `hook_protect_files.py` | You are asked before the CI fixture, the demo GIF, the licence or the code of conduct changes |
| After an edit | `hook_format_python.py` | The edited Python file is formatted with ruff |
| Before a turn ends | `hook_stop_gate.py` | If Python under `src/` or `tests/` changed, the quick checks pass, or Claude is sent back once to fix them |

The hooks need `python3` on your `PATH` and nothing else. They only protect when they can run: if `python3` is missing (often the case on Windows, where the command is `python`), Claude Code reports a hook error and carries on without them. `main` stays safe either way, because the repository's own rules refuse a direct push.

To skip the stop gate for a session, start Claude Code with `SLOPFENCE_SKIP_STOP_GATE=1`.

`settings.json` also lets Claude run the project's own checks and read-only `git` and `gh` commands without asking each time. Personal settings go in `.claude/settings.local.json`, which git ignores.

## Tools

The scripts in [`tools/`](tools) are plain Python you can run yourself:

```bash
python3 .claude/tools/preflight.py                                       # all CI checks
python3 .claude/tools/preflight.py --quick                               # lint, self-check, tests
.venv/bin/python .claude/tools/corpus_run.py --rule SLOP040 --sample 30  # measure a rule
.venv/bin/python .claude/tools/docstring_coverage.py                     # CodeRabbit's docstring check
.venv/bin/python -m pytest .claude/tools -q                              # tests for these scripts
```

## Maintainer mode

`output-styles/maintainer.md` is an optional output style: evidence before claims, plain words, numbers for every detector change. Turn it on with `/output-style maintainer`, or in the desktop app by adding `"outputStyle": "Maintainer"` to `.claude/settings.local.json`.

## Changing this setup

- Keep `CLAUDE.md` short. For each line ask: would removing it cause a mistake? If not, cut it. Put anything that matters only sometimes in a rule or a skill.
- If Claude keeps breaking a rule, don't add emphasis: turn the rule into a hook or a check in `preflight.py`.
- New hook logic gets a test in `tools/test_tools.py`.
