from __future__ import annotations

import argparse
import contextlib
import os
import signal
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from slopfence import __version__, reporters
from slopfence import baseline as baselinemod
from slopfence import diff as diffmod
from slopfence.analysis import default_jobs
from slopfence.config import FAIL_ON, ConfigError, load_config, selected_rules
from slopfence.engine import Result, run
from slopfence.indexes import find_private_index
from slopfence.models import RULES, Severity, plural
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
    baseline = parser.add_mutually_exclusive_group()
    baseline.add_argument(
        "--baseline",
        metavar="FILE",
        type=Path,
        help="hide the findings recorded in FILE and report only new ones (overrides config)",
    )
    baseline.add_argument(
        "--no-baseline",
        action="store_true",
        help="ignore the baseline set in the config and report every finding",
    )
    parser.add_argument(
        "--write-baseline",
        metavar="FILE",
        type=Path,
        help="record the current findings in FILE and report none of them; later runs "
        "with --baseline FILE only report new findings",
    )
    parser.add_argument("--exit-zero", action="store_true", help="exit 0 even if issues are found")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument(
        "--list-rules",
        action="store_true",
        help="list the rules with the severities this project uses, and exit",
    )
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


def _project_root(first_path: Path) -> Path:
    """The project a run belongs to, found from the first path it was given.

    That is the git repository the path is in. Without one, it is the current folder
    when the path is inside it, and else the path's own folder.
    """
    start = first_path.resolve()
    folder = start if start.is_dir() else start.parent
    # --list-rules takes a path that doesn't exist: the nearest folder that does stands in.
    while not folder.is_dir() and folder != folder.parent:
        folder = folder.parent
    root = diffmod.repo_root(folder)
    if root is None:
        cwd = Path.cwd().resolve()
        root = cwd if folder == cwd or cwd in folder.parents else folder
    return root


def _print_rules(severity: Mapping[str, Severity]) -> None:
    """Print every rule with the severity this project uses for it.

    ``severity`` holds what the config sets. A rule it changes also shows its own
    severity, so nobody takes the project's choice for the built-in one.
    """
    for rule in RULES.values():
        level = severity.get(rule.id, rule.severity)
        default = "" if level is rule.severity else f" (default: {rule.severity.value})"
        print(f"{rule.id}  {level.value:<6}  {rule.name}: {rule.summary}{default}")


def _error(message: str) -> int:
    """Say on stderr why the run can't go on, and return the exit code for an error."""
    print(f"slopfence: error: {message}", file=sys.stderr)
    return EXIT_ERROR


def _baseline_to_use(
    given: Path | None, configured: str | None, root: Path
) -> baselinemod.Baseline | None:
    """The baseline whose findings a run hides, or None when it has none.

    ``given`` is ``--baseline``, relative to the current folder. It replaces
    ``configured``, the config key, which is relative to the project root. Raises
    BaselineError when the file is missing or is not a baseline.
    """
    path = root / configured if given is None and configured else given
    return baselinemod.load(path) if path is not None else None


def _record_baseline(path: Path, result: Result) -> None:
    """Write the findings of ``result`` to the baseline ``path``, and report none of them.

    The report then says what the next run with this baseline will say: nothing new.
    Raises OSError when the file can't be written.
    """
    baselinemod.write(path, result.findings)
    result.notes.append(f"Wrote {plural(len(result.findings), 'finding')} to the baseline {path}.")
    if result.unchecked_rules:
        # Otherwise the next run that reaches PyPI fails, and nobody knows why.
        result.notes.append(
            f"{' and '.join(result.unchecked_rules)} findings may be missing from the "
            "baseline: PyPI was not asked, or did not answer, about some packages (offline, "
            "a private package index, or PyPI could not be reached). Write it again when "
            "lookups work."
        )
    result.baselined, result.findings = result.findings, []


def main(argv: Sequence[str] | None = None) -> int:
    """Run slopfence from the command line and return the exit code.

    0 ok, 1 issues, 2 error, 130 stopped with Ctrl-C.
    """
    _use_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.write_baseline is not None and args.diff:
        parser.error("--write-baseline records every finding, so it can't be used with --diff")
    if args.write_baseline is not None and args.baseline is not None:
        parser.error("--write-baseline can't be used with --baseline")

    missing = next((path for path in args.paths if not path.exists()), None)
    # --list-rules checks no file, so it doesn't mind a path that isn't there.
    if missing is not None and not args.list_rules:
        return _error(f"{missing} does not exist")

    root = _project_root(args.paths[0])
    try:
        config = load_config(root)
    except ConfigError as err:
        return _error(str(err))
    if args.list_rules:
        _print_rules(config.severity)
        return EXIT_OK

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

    baseline = None
    if args.write_baseline is not None:
        # A run that writes a baseline uses none: it records every finding. The file
        # is looked at now, so that a mistake doesn't cost a whole run.
        scope = baselinemod.scope_of(args.paths, root, exclude, selected_rules(select, ignore))
        problem = baselinemod.write_problem(args.write_baseline, scope, root)
        if problem:
            return _error(f"can't write baseline {args.write_baseline}: {problem}")
    else:
        configured = None if args.no_baseline else config.baseline
        try:
            baseline = _baseline_to_use(args.baseline, configured, root)
        except baselinemod.BaselineError as err:
            return _error(str(err))

    changed = None
    if args.diff:
        try:
            changed = diffmod.changed_lines(root, args.diff)
        except diffmod.DiffError as err:
            return _error(f"--diff {args.diff}: {err}")

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
            per_file_ignores=config.per_file_ignores,
            severity=config.severity,
            baseline=baseline,
        )
    except KeyboardInterrupt:
        # No report and no cache file: the run did not finish.
        print("slopfence: interrupted", file=sys.stderr)
        _end_like_an_interrupted_program()
        return EXIT_INTERRUPTED
    if isinstance(registry, PyPIRegistry):
        registry.save()

    if args.write_baseline is not None:
        try:
            _record_baseline(args.write_baseline, result)
        except OSError as err:
            return _error(f"can't write baseline {args.write_baseline}: {err.strerror or err}")

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
