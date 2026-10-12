# Contributing to slopfence

Thanks for your interest in slopfence! This project is at an early stage, which is the best time to shape it. Every kind of contribution helps: code, detector ideas, real-world examples, docs, and bug reports.

By participating, you agree to follow our [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to contribute

| You want to… | Do this |
|---|---|
| Share an example of AI-generated junk | Open an issue with the **AI junk example** template |
| Report a false positive | Open an issue with the **False positive** template |
| Propose a new detector | Open an issue with the **Detector proposal** template |
| Report a bug | Open an issue with the **Bug report** template |
| Ask a question or discuss an idea | Start a [Discussion](https://github.com/syedmuhdahmad/slopfence/discussions) |
| Report a security vulnerability | **Don't open an issue.** See [SECURITY.md](SECURITY.md) |
| Fix something or add a feature | Read on |

New here? Look for issues labelled [`good first issue`](https://github.com/syedmuhdahmad/slopfence/labels/good%20first%20issue) or [`help wanted`](https://github.com/syedmuhdahmad/slopfence/labels/help%20wanted).

## Before you start coding

- **Small fixes** (typos, docs, obvious bugs): open a pull request directly.
- **Anything bigger** (new detectors, new features, behaviour changes): open an issue first and wait for a maintainer to agree on the approach. This avoids wasted work.
- Comment on an issue to say you're working on it, so others don't duplicate effort.

## Development workflow

`main` is protected: **nobody pushes to it directly**, maintainers included. All changes go through pull requests.

1. **Fork** the repository and clone your fork.
2. **Create a branch** from `main` with a descriptive name:
   - `feat/slop001-pypi-lookup`
   - `fix/false-positive-mock-assert`
   - `docs/contributing-typo`
3. **Make your changes.** Keep each pull request focused on one thing.
4. **Add tests** for any behaviour change. Detectors need both:
   - cases that **should** be flagged, and
   - similar-looking cases that **should not** be flagged (false-positive guards).
5. **Run checks locally** (see below) and make sure they pass.
6. **Push** to your fork and **open a pull request** against `main`. Fill in the template.

### What happens to your pull request

- The `ci` check must pass. It only passes when all of these pass: lint, tests on Linux, macOS and Windows, the package build (the same build and smoke test as a release), the end-to-end tests of the GitHub Action and the pre-commit hook, and test coverage (line and branch coverage, combined over every Python version and OS, must stay at or above `fail_under` in `pyproject.toml`; the run summary shows a per-file table).
- **CodeRabbit reviews every pull request** automatically and again after each push, and its findings must be fixed before merging. The required `CodeRabbit` check makes sure it has finished reviewing. It requests changes while it has open comments or a failing pre-merge check (a clear title, the linked issue fully addressed, and docstrings on at least 80% of the functions you touch), and approves once they are resolved. If you think a finding is wrong, reply to it: CodeRabbit answers, and a maintainer decides.
- A maintainer merges it once the checks pass and review comments are addressed.
- All review conversations must be resolved.
- Pull requests are **squash-merged**, so `main` has a clean, linear history with one commit per pull request. The pull request title becomes the commit message, so make it clear.
- Your branch is deleted automatically after merge.

## Local checks

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e '.[dev]'

pytest                           # tests
coverage run -m pytest && coverage combine && coverage report   # coverage
ruff check . && ruff format --check .
slopfence src tests              # slopfence must pass on its own code
npx markdownlint-cli2 "**/*.md" "!.venv/**"
```

If you change `.pre-commit-hooks.yaml`, also run the end-to-end hook test (needs `pip install pre-commit`):

```bash
.github/scripts/test-pre-commit-hook.sh .
```

### Working with Claude Code

The repository includes a shared [Claude Code](https://code.claude.com/docs) setup: project instructions in `CLAUDE.md`, coding standards, skills such as `/preflight` and `/add-detector`, review agents, and hooks that format edited files and stop commits on `main`. [`.claude/README.md`](.claude/README.md) lists everything. You don't need Claude Code to contribute, and the same checks run in CI either way.

### Re-recording the demo GIF

`docs/demo.gif` is generated from `docs/demo/demo.tape` with [VHS](https://github.com/charmbracelet/vhs). To re-record it after changing the output, run this (needs Docker):

```bash
docs/demo/record.sh
```

The demo project is created at record time by `docs/demo/make-project.sh`, so no sloppy files live in the repo.

### Adding a detector

1. Add the rule to `RULES` in `src/slopfence/models.py`.
2. Write the detector in `src/slopfence/detectors/` and register it in `detectors/__init__.py`.
3. Give each `Finding` a `subject`: what it is about (a package, function or test name, or the text of the flagged comment). Baselines match findings on it, so it must not hold a line number or another file's location. Add one finding of your rule to the `project` fixture in `tests/test_baseline.py`, its subject to `SUBJECTS`, and its row to the list in `test_baseline_file_is_sorted_stable_and_ascii`. A test fails until you do. No other test there counts the findings by hand. Never change the subject of an existing rule without raising `BASELINE_VERSION` in `baseline.py`.
4. Add tests with **both** flagged and not-flagged cases. Real-world code that must stay quiet is the most valuable test.
5. Run it against a large codebase (for example your Python install's standard library) and check for false positives before opening the pull request.

Per-file detectors run in worker processes. Write a detector as a plain function of the `SourceFile`: no global state, no network. To visit every node, loop over `src.nodes` instead of calling `ast.walk(src.tree)`. The tree is then walked only once per file.

## Commit and pull request style

- Use the imperative mood: "Add PyPI lookup cache", not "Added" or "Adds".
- Pull request titles follow [Conventional Commits](https://www.conventionalcommits.org/) style where it fits:
  - `feat: add SLOP010 placeholder comment detector`
  - `fix: ignore mocks used as fixtures in SLOP020`
  - `docs: clarify ignore comment syntax`
  - `chore: bump actions/checkout`
- Reference related issues: `Closes #12`.

## Writing a good detector

A detector is only useful if people trust it. Before proposing or writing one, check:

1. **Is it objective?** Could two reviewers agree on whether a given snippet matches?
2. **Is it high-confidence?** It's better to miss some cases than to flag correct code.
3. **Is it actionable?** The message should tell the user what's wrong and how to fix it.
4. **Is it about the pattern, not the author?** slopfence never tries to guess whether AI wrote the code.

## Licensing of contributions

By submitting a pull request, you agree that your contribution is licensed under the [MIT License](LICENSE), the same license as the project.

## Recognition

All contributors are credited in release notes. Thank you for helping make AI-assisted code better!
