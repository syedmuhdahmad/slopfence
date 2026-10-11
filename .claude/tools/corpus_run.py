#!/usr/bin/env python3
"""Run slopfence rules over a large body of real code and sample the findings.

A detector is tuned by reading what it flags in code nobody wrote for the test
suite. This prints how often the rules fire and a reproducible random sample to
classify by hand.

Run it with the project's virtualenv, so it measures the code in this checkout:
    .venv/bin/python .claude/tools/corpus_run.py --rule SLOP040 --sample 30

With no paths, the corpus is the standard library of the Python that runs the script.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import sysconfig
import time
from collections import Counter
from pathlib import Path

from slopfence.engine import run
from slopfence.models import RULES, Finding
from slopfence.registry import OfflineRegistry

# Lines of source shown above and below a sampled finding.
CONTEXT_LINES = 2


def scan(roots: list[Path], rules: list[str]) -> tuple[list[tuple[Path, Finding]], int]:
    """Run ``rules`` on every root; return (root, finding) pairs and the number of files checked."""
    findings: list[tuple[Path, Finding]] = []
    files = 0
    for root in roots:
        # Offline: SLOP001 would otherwise look up thousands of names on PyPI.
        result = run([root], root, OfflineRegistry(), select=rules)
        files += result.files_checked
        findings.extend((root, finding) for finding in result.findings)
    return findings, files


def excerpt(root: Path, finding: Finding) -> str:
    """The flagged line with a little context, the flagged line marked with ``>``."""
    try:
        lines = (root / finding.path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "    (source not readable)"
    first = max(finding.line - CONTEXT_LINES, 1)
    last = min(finding.line + CONTEXT_LINES, len(lines))
    return "\n".join(
        f"  {'>' if number == finding.line else ' '} {number:5}  {lines[number - 1]}"
        for number in range(first, last + 1)
    )


def main(argv: list[str] | None = None) -> int:
    """Print counts and a random sample of findings; optionally save every finding as JSON."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", type=Path, help="folders of Python code to scan")
    parser.add_argument("--rule", action="append", required=True, help="rule ID; repeatable")
    parser.add_argument("--sample", type=int, default=30, help="findings to show (default 30)")
    parser.add_argument("--seed", type=int, default=0, help="seed for the sample (default 0)")
    parser.add_argument("--json", type=Path, help="write every finding to this file")
    args = parser.parse_args(argv)

    rules = [rule.upper() for rule in args.rule]
    unknown = [rule for rule in rules if rule not in RULES]
    if unknown:
        parser.error(f"unknown rule(s): {', '.join(unknown)}")
    roots = [p.resolve() for p in args.paths] or [Path(sysconfig.get_paths()["stdlib"])]
    missing = [str(root) for root in roots if not root.is_dir()]
    if missing:
        parser.error(f"not a folder: {', '.join(missing)}")

    started = time.perf_counter()
    findings, files = scan(roots, rules)
    seconds = time.perf_counter() - started

    flagged_files = {(root, finding.path) for root, finding in findings}
    per_thousand = 1000 * len(findings) / files if files else 0.0
    print(f"corpus:    {', '.join(map(str, roots))}")
    print(f"files:     {files} checked in {seconds:.1f}s")
    print(
        f"findings:  {len(findings)} in {len(flagged_files)} files ({per_thousand:.2f} per 1,000)"
    )
    for rule, count in sorted(Counter(finding.rule for _, finding in findings).items()):
        print(f"  {rule}: {count}")

    if args.json:
        rows = [
            {
                "root": str(root),
                "rule": f.rule,
                "path": f.path,
                "line": f.line,
                "message": f.message,
            }
            for root, f in findings
        ]
        args.json.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"saved:     {args.json}")

    sample = random.Random(args.seed).sample(findings, min(args.sample, len(findings)))
    print(f"\nsample of {len(sample)} (seed {args.seed}):")
    for number, (root, finding) in enumerate(sample, 1):
        print(f"\n[{number}] {finding.rule}  {root / finding.path}:{finding.line}")
        print(f"    {finding.message}")
        print(excerpt(root, finding))
    return 0


if __name__ == "__main__":
    sys.exit(main())
