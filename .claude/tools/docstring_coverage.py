#!/usr/bin/env python3
"""Share of the functions changed since a base branch that have a docstring.

CodeRabbit blocks a pull request when fewer than 80% of the functions it touches
have docstrings. This gives the same number before the pull request exists.

Run it with the project's virtualenv, because it reuses slopfence's own diff reader:
    .venv/bin/python .claude/tools/docstring_coverage.py --base origin/main --min 80
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

from slopfence import diff

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def touched_functions(root: Path, changed: diff.ChangedLines) -> list[tuple[str, FunctionNode]]:
    """Every function in a changed Python file that overlaps a changed line."""
    found = []
    for rel in sorted(changed):
        path = root / rel
        if path.suffix != ".py" or not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, ValueError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, FunctionNode) and diff.touches(
                changed, rel, node.lineno, node.end_lineno
            ):
                found.append((rel, node))
    return found


def main(argv: list[str] | None = None) -> int:
    """Print the docstring share of touched functions; return 1 when it is below ``--min``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main", help="branch to compare with")
    parser.add_argument("--min", type=float, default=80.0, help="lowest share that passes")
    args = parser.parse_args(argv)

    root = diff.repo_root(Path.cwd())
    if root is None:
        print("docstring coverage: not inside a git repository", file=sys.stderr)
        return 2
    try:
        changed = diff.changed_lines(root, args.base)
    except diff.DiffError as err:
        print(f"docstring coverage: cannot compare with {args.base}: {err}", file=sys.stderr)
        return 2

    functions = touched_functions(root, changed)
    if not functions:
        print(f"No functions changed since {args.base}.")
        return 0
    missing = [(rel, node) for rel, node in functions if ast.get_docstring(node) is None]
    share = 100 * (len(functions) - len(missing)) / len(functions)
    print(
        f"{len(functions) - len(missing)} of {len(functions)} touched functions "
        f"have a docstring ({share:.0f}%, minimum {args.min:.0f}%)"
    )
    for rel, node in missing:
        print(f"  {rel}:{node.lineno}  {node.name}")
    return 0 if share >= args.min else 1


if __name__ == "__main__":
    sys.exit(main())
