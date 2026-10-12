"""Baseline files: accept the findings a project has today and report only new ones.

A baseline lists findings as ``[path, rule, fingerprint, count]``. The fingerprint is
what the finding is about (``Finding.subject``: a package, function or test name, or
the text of the flagged comment). It holds no line number, so a baselined finding stays
hidden when code moves. ``count`` says how many findings in the file share the
fingerprint.

Nothing here parses source code or reads a checked file: the findings carry all that
is needed.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from slopfence.config import is_excluded
from slopfence.diff import ChangedLines, touches
from slopfence.files import replace_text
from slopfence.models import RULES, Finding, plural
from slopfence.source import relative_path

# Raise this when the file format, the way a fingerprint is normalized or the subject
# of an existing rule changes. An old file then fails with "re-create it", instead of
# every finding in it being reported as new.
BASELINE_VERSION = 1
# A longer fingerprint is cut and ends in a hash of the whole text, so rows stay short.
MAX_FINGERPRINT = 120
_HASH_CHARS = 12
_NOT_A_BASELINE = (
    "the file exists and is not a slopfence baseline (delete it first, or choose another name)"
)
# What to do about a baseline that ``names_no_file``. {} is the project root.
_NO_FILE_HERE = (
    "no file that exists in {}. A baseline's paths are relative to the project root, and "
    "without a git repository the root depends on where slopfence is run. Run slopfence "
    "from the folder the baseline was written from"
)

# (project-relative POSIX path, rule ID, fingerprint)
Key = tuple[str, str, str]


class BaselineError(ValueError):
    """The baseline file is missing, or it is not a baseline this version can read."""


@dataclass(frozen=True)
class Baseline:
    """The findings a project has accepted: how many with each key may be hidden."""

    entries: Mapping[Key, int]


@dataclass(frozen=True)
class Match:
    """Findings split by a baseline. Both lists keep the order the findings came in."""

    new: list[Finding]
    hidden: list[Finding]
    # The entries, or the part of their count, that no finding used.
    unused: dict[Key, int]


@dataclass(frozen=True)
class Scope:
    """What a run checks: the paths it was given, without the files and rules it leaves out."""

    # Named the way findings name their files: see ``scope_of``.
    paths: Sequence[str]
    # Patterns of files that are not checked, as ``config.is_excluded`` takes them.
    exclude: Sequence[str] = ()
    # The rules of this version that do not run.
    skipped: Collection[str] = frozenset()


def normalize_fingerprint(text: str) -> str:
    """Collapse whitespace, and cut very long text down to its start plus a hash."""
    key = " ".join(text.split())
    if len(key) > MAX_FINGERPRINT:
        digest = hashlib.sha256(key.encode("utf-8", "backslashreplace")).hexdigest()
        keep = MAX_FINGERPRINT - _HASH_CHARS - 3
        key = f"{key[:keep]}...{digest[:_HASH_CHARS]}"
    return key


def key_of(finding: Finding) -> Key:
    """The baseline key of a finding: its path, its rule and its fingerprint.

    The fingerprint is the finding's subject. Every rule sets one. A finding built
    without one falls back on its message, which errs on the side of reporting: it
    stops matching when the message changes, and it never hides another finding.
    """
    text = finding.message if finding.subject is None else finding.subject
    return finding.path, finding.rule, normalize_fingerprint(text)


def count_findings(findings: Iterable[Finding]) -> dict[Key, int]:
    """The entries of a baseline that holds ``findings``: each key with how often it occurs."""
    return dict(Counter(key_of(finding) for finding in findings))


def dumps(entries: Mapping[Key, int]) -> str:
    """The text of a baseline file: one finding per line, sorted, ASCII only.

    The same findings give the same bytes on every platform and Python version, and
    one finding more or less is one line more or less. So the file diffs and merges
    well in git.
    """
    rows = [
        "    " + json.dumps([path, rule, fingerprint, count])
        for (path, rule, fingerprint), count in sorted(entries.items())
    ]
    body = "\n" + ",\n".join(rows) + "\n  " if rows else ""
    return f'{{\n  "version": {BASELINE_VERSION},\n  "findings": [{body}]\n}}\n'


def write(path: Path, findings: Iterable[Finding]) -> None:
    """Write a baseline that holds ``findings``. Raises OSError if that fails.

    The file is replaced in one step, so a run that fails or is stopped never leaves
    half a baseline behind.
    """
    # realpath: write through a symbolic link instead of putting a file in its place.
    replace_text(Path(os.path.realpath(path)), dumps(count_findings(findings)))


def _is_row(row: object) -> bool:
    """Whether a row of the file is ``[path, rule, fingerprint, count]``, the count 1 or more."""
    return (
        isinstance(row, list)
        and len(row) == 4
        and all(isinstance(value, str) for value in row[:3])
        # bool is a kind of int in Python, so ``true`` has to be turned down by hand.
        and isinstance(row[3], int)
        and not isinstance(row[3], bool)
        and row[3] >= 1
    )


def _version(data: object) -> int | None:
    """The format version of a parsed baseline file, or None if it has none."""
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, int) and not isinstance(version, bool) else None


def _parse(data: object, path: Path) -> Baseline:
    """Turn a parsed baseline file into a Baseline. Raises BaselineError if it isn't one."""
    version = _version(data)
    if not isinstance(data, dict) or version is None:
        raise BaselineError(f"baseline {path} is not a slopfence baseline file")
    if version != BASELINE_VERSION:
        raise BaselineError(
            f"baseline {path} has format version {version}, but this slopfence reads "
            f"version {BASELINE_VERSION}; re-create it with --write-baseline"
        )
    rows = data.get("findings")
    if not isinstance(rows, list) or not all(_is_row(row) for row in rows):
        raise BaselineError(
            f"baseline {path} is not a slopfence baseline file: 'findings' must be a "
            "list of [path, rule, fingerprint, count]"
        )
    entries: Counter[Key] = Counter()
    for file, rule, fingerprint, count in rows:
        # The same key twice (a file merged by hand) adds up.
        entries[(file, rule, fingerprint)] += count
    return Baseline(dict(entries))


def _read_text(path: Path) -> str:
    """The text of a baseline file. Raises OSError, or ValueError if it is not UTF-8."""
    # utf-8-sig: an editor may have put a byte order mark in front.
    return path.read_text(encoding="utf-8-sig")


def load(path: Path) -> Baseline:
    """Read a baseline file. Raises BaselineError if it is missing or not a baseline.

    A row for a rule this version doesn't know is kept. It matches nothing, so a
    baseline written by a newer slopfence doesn't break an older one.
    """
    try:
        data = json.loads(_read_text(path))
    except FileNotFoundError as err:
        raise BaselineError(
            f"baseline {path} does not exist (create it with --write-baseline)"
        ) from err
    except OSError as err:
        raise BaselineError(f"baseline {path}: {err.strerror or err}") from err
    except RecursionError as err:  # thousands of brackets inside each other
        raise BaselineError(f"baseline {path} is not valid JSON: nested too deeply") from err
    except ValueError as err:  # not JSON, or not UTF-8
        raise BaselineError(f"baseline {path} is not valid JSON: {err}") from err
    return _parse(data, path)


def write_problem(path: Path, scope: Scope, root: Path) -> str | None:
    """Why ``--write-baseline`` must not write to ``path``, or None if it may.

    Asked before the scan, so that a mistake costs neither a full run nor a file.
    The folder must exist, and it must be writable: the file is replaced through a
    temporary file next to it (see ``write``). A file that is there must be writable
    too, and it must be empty or a baseline: a forgotten file name
    (``--write-baseline src/app.py``) must not replace source code. It must be a
    baseline of this project (see ``names_no_file``). And replacing it must not drop
    findings that a run with ``scope`` does not check (see ``uncovered``).
    """
    # os.path, not Path: none of these raises, whatever was typed as the file name.
    if not os.path.isdir(os.path.dirname(os.path.abspath(path))):
        return f"the folder {path.parent} does not exist"
    if os.path.isdir(path):
        return "it is a folder"
    if os.path.exists(path) and not os.path.isfile(path):
        # A named pipe or a device. Reading a pipe would wait for ever.
        return "it is not a regular file"
    # realpath: a symbolic link is written through, into the folder of its target.
    if not os.access(os.path.dirname(os.path.realpath(path)), os.W_OK):
        return "no permission to write in its folder"
    if os.path.exists(path) and not os.access(path, os.W_OK):
        # Replacing the file would get around its protection, and lose it.
        return "no permission to write to it"
    try:
        text = _read_text(path)
        if not text.strip():
            return None  # an empty file holds nothing that could be lost
        data = json.loads(text)
    except FileNotFoundError:
        return None
    except OSError as err:
        return err.strerror or str(err)
    except (ValueError, RecursionError):  # not UTF-8, or not JSON
        return _NOT_A_BASELINE
    if _version(data) is None or not isinstance(data.get("findings"), list):
        return _NOT_A_BASELINE
    try:
        old = _parse(data, path)
    except BaselineError:
        return None  # another format version or damaged rows: nothing of use to lose
    if names_no_file(old.entries, root):
        return f"it names {_NO_FILE_HERE.format(root)}, or delete the file first"
    lost = uncovered(old.entries, scope, root)
    if lost:
        example, rule, _ = min(lost)
        return (
            f"it holds {plural(sum(lost.values()), 'finding')} that this run does not "
            f"check (for example {rule} in {example}). Check everything the baseline "
            "holds, or delete the file first"
        )
    return None


def match(
    findings: Sequence[Finding], baseline: Baseline, changed: ChangedLines | None = None
) -> Match:
    """Split ``findings`` (in report order) into new ones and ones the baseline hides.

    Each entry hides as many findings as its count. When more findings share a key,
    the first ones in the file are hidden. With ``changed`` (``--diff``), findings
    outside the changed lines use up the entries first: they are the old ones, so a
    new one on a changed line is the one that gets reported.

    A finding with a ``peer`` (SLOP030 with ``--diff``: a changed function that is the
    first copy of a group of identical ones, or was until an edit made it a near-copy)
    has no entry of its own, because a full scan never reports the first copy. An entry
    with its key was written for another function, so it leaves that one alone. It is
    hidden when its peer has an entry, and that entry stays whole for the peer's own
    finding. An entry that already can't hide every old finding with its key stands for
    nothing more: something with that key is new.
    """

    def touched(i: int) -> bool:
        """Whether finding ``i`` is on a changed line. Never, without ``--diff``."""
        f = findings[i]
        return changed is not None and touches(changed, f.path, f.line, f.end_line)

    keys = [key_of(finding) for finding in findings]
    # A stable sort: untouched findings first, each group still in report order.
    order = sorted(range(len(findings)), key=touched)
    remaining = dict(baseline.entries)
    hidden_at: set[int] = set()
    for i in order:
        if findings[i].peer is None and remaining.get(keys[i], 0) > 0:
            remaining[keys[i]] -= 1
            hidden_at.add(i)
    # The keys with more old findings than their entry hides.
    short = {keys[i] for i in range(len(findings)) if i not in hidden_at and not touched(i)}
    for i, finding in enumerate(findings):
        if i not in hidden_at and finding.peer is not None:
            path, subject = finding.peer
            peer_key = (path, finding.rule, normalize_fingerprint(subject))
            if peer_key in baseline.entries and peer_key not in short:
                hidden_at.add(i)
    return Match(
        new=[f for i, f in enumerate(findings) if i not in hidden_at],
        hidden=[f for i, f in enumerate(findings) if i in hidden_at],
        unused={key: count for key, count in remaining.items() if count > 0},
    )


def scope_of(
    paths: Iterable[Path],
    root: Path,
    exclude: Sequence[str] = (),
    rules: Collection[str] | None = None,
) -> Scope:
    """What a run on ``paths`` checks when it skips ``exclude`` and runs ``rules`` (None: all).

    The paths are named the way findings name their files: a project-relative POSIX
    path, "." for the project itself, and an absolute path for anything outside the
    project.
    """
    return Scope(
        paths=sorted({relative_path(Path(path).resolve(), root) for path in paths}),
        exclude=tuple(exclude),
        skipped=frozenset(() if rules is None else RULES.keys() - set(rules)),
    )


def _exists(root: Path, path: str) -> bool:
    """Whether the file of a baseline row is still there. Never raises, whatever the row holds."""
    # os.path.exists answers False to any error. On older Python versions Path.exists
    # passes some on (a name that is too long), and a row is data that anyone may edit.
    return os.path.exists(os.path.join(root, path))


def _outside_project(path: str) -> bool:
    """Whether a baseline row names a file outside the project.

    A file in the project is named relative to it. Anything else is outside: an
    absolute path, and a relative one that climbs out with "..". The text of the row
    decides, by the rules of every system at once, so a baseline is read the same way
    everywhere. ``os.path.isabs`` would not do: a Windows path (C:/x) is relative on Linux,
    and from Python 3.13 on a Linux path (/x) is relative on Windows.
    """
    return (
        path.startswith(("/", "\\"))
        or bool(ntpath.splitdrive(path)[0])
        # ntpath takes both "/" and "\\" for a separator.
        or ntpath.normpath(path).split("\\")[0] == ".."
    )


def names_no_file(entries: Mapping[Key, int], root: Path) -> bool:
    """Whether a baseline names files of the project and not one of them exists.

    Then it was most likely written for another folder: every path in the file is
    relative to the project root, and without a git repository that is the folder
    slopfence is run from (see ``cli._project_root``). Or the files are all gone.
    Nobody can tell the two apart, so such a baseline is neither called out of date
    nor replaced.
    """
    inside = {path for path, _, _ in entries if not _outside_project(path)}
    return bool(inside) and not any(_exists(root, path) for path in inside)


def _in_paths(path: str, paths: Collection[str]) -> bool:
    """Whether the file of a baseline row is one of, or inside one of, the paths of a run."""
    return any(
        path == given
        or path.startswith(given + "/")
        # The project itself holds every file that is named relative to it.
        or (given == "." and not _outside_project(path))
        for given in paths
    )


def uncovered(entries: Mapping[Key, int], scope: Scope, root: Path) -> dict[Key, int]:
    """The baselined findings that a run with ``scope`` can't find again.

    That is a finding of a rule that does not run, and a finding of a file that
    exists but is outside the paths of the run or excluded from it. The run can't
    tell whether those findings are still there, and a baseline written from it would
    lose them. A finding of a file that is gone is lost already.

    A file outside the project (see ``_outside_project``) is taken to exist. Nothing is
    looked at outside the project unless the run was asked to check it: a row is data
    that anyone may edit, and on Windows a path can name another computer.
    """
    unchecked: dict[str, bool] = {}

    def is_unchecked(path: str) -> bool:
        """Whether the file exists, as far as anyone may look, and the run leaves it out."""
        if path not in unchecked:
            left_out = not _in_paths(path, scope.paths) or is_excluded(path, scope.exclude)
            unchecked[path] = left_out and (_outside_project(path) or _exists(root, path))
        return unchecked[path]

    return {
        key: entries[key]
        for key in sorted(entries)
        if key[1] in scope.skipped or is_unchecked(key[0])
    }


def count_stale(
    unused: Mapping[Key, int],
    rules: Collection[str],
    checked: Collection[str],
    scope: Scope,
    root: Path,
) -> int:
    """How many baselined findings this run should have found again, and did not.

    ``unused`` is what ``match`` left over. ``rules`` are the rules whose findings a
    run can be trusted to find again, ``checked`` the files that were checked, and
    ``scope`` what the run set out to check.

    An entry only counts when its rule is one of ``rules`` and its file was checked
    or is gone. And nothing counts unless the run covers the baseline. That means no
    unused entry is for something the run left out (see ``uncovered``), or for a file
    that is there but was not checked after all, because it is not valid Python. The
    count comes with the advice to write the baseline again, and only then does that
    lose nothing.
    """
    if uncovered(unused, scope, root):
        return 0
    gone = 0
    for (path, rule, _), count in unused.items():
        if rule not in rules:
            continue
        if path not in checked and _exists(root, path):
            return 0
        gone += count
    return gone


def stale_note(
    baseline: Baseline,
    unused: Mapping[Key, int],
    rules: Collection[str],
    checked: Collection[str],
    scope: Scope,
    root: Path,
) -> str | None:
    """What to tell the user about baselined findings that a run did not find again.

    None when there is nothing to say. ``unused`` and the arguments after it are those
    of ``count_stale``.
    """
    if names_no_file(baseline.entries, root):
        return (
            f"The baseline names {_NO_FILE_HERE.format(root)}. If its files are gone for "
            "good, delete the baseline and write it again."
        )
    gone = count_stale(unused, rules, checked, scope, root)
    if not gone:
        return None
    return (
        f"{plural(gone, 'finding')} in the baseline no longer exist{'s' if gone == 1 else ''}. "
        "Run --write-baseline again to update the file."
    )
