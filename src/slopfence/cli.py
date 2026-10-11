from __future__ import annotations

import argparse
import contextlib
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from slopfence import __version__, reporters
from slopfence import diff as diffmod
from slopfence.analysis import default_jobs
from slopfence.config import FAIL_ON, ConfigError, load_config
from slopfence.engine import run
from slopfence.indexes import find_private_index
from slopfence.models import RULES, Severity
from slopfence.registry import OfflineRegistry, PyPIRegistry

EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2
# What a shell reports for a program stopped with Ctrl-C (128 + SIGINT).
EXIT_INTERRUPTED = 130


def _rule_list(value: str) -> list[str]:
    rules = [r.strip().upper() for r in value.split(",") if r.strip()]
    unknown = [r for r in rules if r not in RULES]
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown rule(s): {', '.join(unknown)}")
    return rules


def _jobs(value: str) -> int:
    """Parse ``--jobs``: a whole number, 1 or more."""
    try:
        jobs = int(value)
    except ValueError:
        jobs = 0
    if jobs < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number, 1 or more (got {value!r})")
    return jobs


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser. Options left unset default to None so config can fill them."""
    parser = argparse.ArgumentParser(
        prog="slopfence",
        description="Find the junk AI coding assistants leave behind.",
    )
    parser.add_argument(
        "paths", nargs="*", type=Path, default=[Path(".")], help="files or directories"
    )
    parser.add_argument(
        "--diff", metavar="REF", help="only report issues on lines changed since REF"
    )
    parser.add_argument("--format", choices=["text", "json", "sarif"], default="text")
    parser.add_argument("-o", "--output", type=Path, help="write the report to a file")
    parser.add_argument(
        "--offline", action="store_true", help="make no network requests (disables SLOP001)"
    )
    parser.add_argument(
        "--select", type=_rule_list, help="comma-separated rules to run (overrides config)"
    )
    parser.add_argument(
        "--ignore", type=_rule_list, help="comma-separated rules to skip (overrides config)"
    )
    parser.add_argument(
        "--exclude",
        type=lambda v: [p.strip() for p in v.split(",") if p.strip()],
        help="comma-separated paths or globs to skip (overrides config)",
    )
    parser.add_argument(
        "--known-packages",
        type=lambda v: [p.strip() for p in v.split(",") if p.strip()],
        help="comma-separated private package names or globs that SLOP001 must accept "
        "(overrides config)",
    )
    parser.add_argument(
        "--check-imports",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="also look up unknown imports in source code on PyPI; this sends their names to "
        "pypi.org (default: off, only dependency files are checked; --no-check-imports "
        "overrides config)",
    )
    parser.add_argument(
        "--detect-private-index",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="skip PyPI lookups when pip or uv is configured with a non-PyPI index "
        "(PIP_INDEX_URL, pip.conf, uv.toml...), since missing packages may be private "
        "(default: on; --no-detect-private-index if that index only mirrors PyPI)",
    )
    parser.add_argument(
        "--fail-on",
        choices=FAIL_ON,
        help="lowest severity that makes the run fail (default: low, i.e. any issue)",
    )
    parser.add_argument(
        "--strict",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="exit 2 if any Python file can't be parsed (--no-strict overrides config)",
    )
    parser.add_argument(
        "--jobs",
        type=_jobs,
        metavar="N",
        help="check files in up to N processes (default: one per CPU, at most 8; "
        "1 uses no worker processes; overrides config)",
    )
    parser.add_argument("--exit-zero", action="store_true", help="exit 0 even if issues are found")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--list-rules", action="store_true", help="list rules and exit")
    parser.add_argument("--version", action="version", version=f"slopfence {__version__}")
    return parser


def _use_utf8_output() -> None:
    """Write UTF-8 even where Python defaults to a legacy code page (Windows CI pipes).

    Otherwise a finding that quotes an emoji or other non-cp1252 text crashes the run.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("_", "-")
        if encoding not in ("utf-8", "utf8") and hasattr(stream, "reconfigure"):
            with contextlib.suppress(ValueError, OSError):
                stream.reconfigure(encoding="utf-8", errors="replace")


def _end_like_an_interrupted_program() -> None:
    """End this process the way Ctrl-C ends a program that doesn't catch it.

    A shell stops a script or a loop only when the program it ran died of the
    interrupt. After a plain exit with status 130 it runs the next command. So the
    interrupt gets its default action back and is sent again. A shell still shows 130.

    Windows has no such rule. There this does nothing, and the caller exits with 130.
    """
    if os.name == "posix":
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.raise_signal(signal.SIGINT)


def main(argv: Sequence[str] | None = None) -> int:
    """Run slopfence from the command line and return the exit code.

    0 ok, 1 issues, 2 error, 130 stopped with Ctrl-C.
    """
    _use_utf8_output()
    args = build_parser().parse_args(argv)

    if args.list_rules:
        for rule in RULES.values():
            print(f"{rule.id}  {rule.severity.value:<6}  {rule.name}: {rule.summary}")
        return EXIT_OK

    for path in args.paths:
        if not path.exists():
            print(f"slopfence: error: {path} does not exist", file=sys.stderr)
            return EXIT_ERROR

    start = args.paths[0].resolve()
    start_dir = start if start.is_dir() else start.parent
    root = diffmod.repo_root(start_dir)
    if root is None:
        cwd = Path.cwd().resolve()
        root = cwd if start_dir == cwd or cwd in start_dir.parents else start_dir

    try:
        config = load_config(root)
    except ConfigError as err:
        print(f"slopfence: error: {err}", file=sys.stderr)
        return EXIT_ERROR
    # Command-line options replace the matching config values.
    select = args.select if args.select is not None else config.select
    ignore = args.ignore if args.ignore is not None else config.ignore
    exclude = args.exclude if args.exclude is not None else config.exclude
    known_packages = (
        args.known_packages if args.known_packages is not None else config.known_packages
    )
    check_imports = args.check_imports if args.check_imports is not None else config.check_imports
    detect_private_index = (
        args.detect_private_index
        if args.detect_private_index is not None
        else config.detect_private_index
    )
    fail_on = Severity(args.fail_on or config.fail_on)
    strict = args.strict if args.strict is not None else config.strict

    changed = None
    if args.diff:
        try:
            changed = diffmod.changed_lines(root, args.diff)
        except diffmod.DiffError as err:
            print(f"slopfence: error: --diff {args.diff}: {err}", file=sys.stderr)
            return EXIT_ERROR

    jobs = args.jobs if args.jobs is not None else config.jobs
    if jobs is None:
        jobs = default_jobs()

    registry = OfflineRegistry() if args.offline else PyPIRegistry()
    private_index = find_private_index(root) if detect_private_index and not args.offline else None
    try:
        result = run(
            args.paths,
            root,
            registry,
            select=select,
            ignore=ignore,
            changed=changed,
            exclude=exclude,
            known_packages=known_packages,
            check_imports=check_imports,
            private_index=private_index,
            jobs=jobs,
        )
    except KeyboardInterrupt:
        # No report and no cache file: the run did not finish.
        print("slopfence: interrupted", file=sys.stderr)
        _end_like_an_interrupted_program()
        return EXIT_INTERRUPTED
    if isinstance(registry, PyPIRegistry):
        registry.save()

    if args.format == "json":
        report = reporters.as_json(result)
    elif args.format == "sarif":
        report = reporters.sarif(result, strict=strict)
    else:
        color = (
            not args.no_color
            and args.output is None
            and sys.stdout.isatty()
            and "NO_COLOR" not in os.environ
        )
        report = reporters.text(result, color=color, strict=strict)

    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
    else:
        print(report)

    if args.exit_zero:
        return EXIT_OK
    if strict and result.parse_errors:
        return EXIT_ERROR
    if any(f.severity.rank >= fail_on.rank for f in result.findings):
        return EXIT_FINDINGS
    return EXIT_OK
