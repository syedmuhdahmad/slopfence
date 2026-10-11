---
paths:
  - "src/slopfence/detectors/**/*.py"
  - "src/slopfence/models.py"
---

# Detector rules

A detector is only useful if people trust it. Before you add or change one:

- **Flag the pattern, never the author.** slopfence does not guess whether AI wrote the code.
- **Be objective.** Two reviewers must agree on whether a snippet matches.
- **Prefer silence to a wrong finding.** When a case is unclear, don't flag it. Missing some junk is fine; flagging correct code is not.
- **Write tests both ways:** cases that SHOULD be flagged, and look-alike cases that should NOT. Real-world code that must stay quiet is the most valuable test.
- **Measure on real code before the pull request.** Run the rule on a large corpus (the `corpus-run` skill), read a random sample of findings, and put the numbers in the pull request. Every change of approach should come from reading real findings.
- **Messages say what is wrong and what to do**, and name the thing involved: `Function 'x' is identical to 'y' in a.py:3; reuse it instead of keeping two copies`.
- **Skip code where the pattern is legitimate**, usually tests (`src.is_test_file`) and examples (`src.is_example_file`).
- **Don't handle ignore comments or `--diff` in a detector.** The engine filters findings.
- **Severity:** high for something almost certainly wrong and harmful, medium for a likely problem, low for a deliberate-but-risky habit.

## Checklist for a new rule

1. The issue agrees the approach.
2. `models.RULES` has the rule, and `detectors/__init__.py` registers the function.
3. Tests cover flagged and not-flagged cases.
4. A corpus run, with a hand-checked sample.
5. README: the detector table, and "What the code-quality rules skip" if the rule has exemptions.
6. `CHANGELOG.md` under `[Unreleased]`.
7. `slopfence src tests` is still clean.
