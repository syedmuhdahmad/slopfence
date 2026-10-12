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
import os
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from slopfence.diff import ChangedLines, touches
from slopfence.files import replace_text
from slopfence.models import Finding, plural
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


def write_problem(path: Path, scope: Collection[str], root: Path) -> str | None:
    """Why ``--write-baseline`` must not write to ``path``, or None if it may.

    Asked before the scan, so that a mistake costs neither a full run nor a file.
    The folder must exist. A file that is there must be empty or a baseline: a
    forgotten file name (``--write-baseline src/app.py``) must not replace source
    code. And replacing it must not drop findings for files outside ``scope``, the
    paths this run checks (see ``scope_of``).
    """
    # os.path, not Path: these two never raise, whatever was typed as the file name.
    if not os.path.isdir(os.path.dirname(os.path.abspath(path))):
        return f"the folder {path.parent} does not exist"
    if os.path.isdir(path):
        return "it is a folder"
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
    lost = uncovered(old.entries, scope, root)
    if lost:
        return (
            f"it holds {plural(sum(lost.values()), 'finding')} for files outside the "
            f"checked paths (for example {min(lost)}). Check the whole project, or delete "
            "the file first"
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


def scope_of(paths: Iterable[Path], root: Path) -> list[str]:
    """The paths a run was given, named the way findings name their files.

    That is a project-relative POSIX path, "." for the project itself, and an
    absolute path for anything outside the project.
    """
    return sorted({relative_path(Path(path).resolve(), root) for path in paths})


def _exists(root: Path, path: str) -> bool:
    """Whether the file of a baseline row is still there. Never raises, whatever the row holds."""
    # os.path.exists answers False to any error. On older Python versions Path.exists
    # passes some on (a name that is too long), and a row is data that anyone may edit.
    return os.path.exists(os.path.join(root, path))


def _in_scope(path: str, scope: Collection[str]) -> bool:
    """Whether the file of a baseline row is one of, or inside one of, the paths of a run."""
    return any(
        path == given
        or path.startswith(given + "/")
        # The project itself holds every file that is named relative to it.
        or (given == "." and not os.path.isabs(path))
        for given in scope
    )


def uncovered(entries: Mapping[Key, int], scope: Collection[str], root: Path) -> dict[str, int]:
    """The baselined findings of each file that exists but lies outside ``scope``.

    A run with that scope can't tell whether those findings are still there, and a
    baseline written from it would lose them.

    A file outside the project (an absolute path) is taken to exist. Nothing is looked
    at outside the project unless the run was asked to check it: a row is data that
    anyone may edit, and on Windows a path can name another computer.
    """
    outside: Counter[str] = Counter()
    for (path, _, _), count in entries.items():
        if not _in_scope(path, scope):
            outside[path] += count
    return {
        path: count
        for path, count in sorted(outside.items())
        if os.path.isabs(path) or _exists(root, path)
    }


def count_stale(
    unused: Mapping[Key, int],
    rules: Collection[str],
    checked: Collection[str],
    scope: Collection[str],
    root: Path,
) -> int:
    """How many baselined findings this run should have found again, and did not.

    ``unused`` is what ``match`` left over. ``rules`` are the rules whose findings a
    run can be trusted to find again, ``checked`` the files that were checked, and
    ``scope`` the paths the run was given.

    An entry only counts when its rule is one of ``rules`` and its file was checked
    or is gone. And nothing counts unless the run covers the baseline, which means
    that no unused entry is for a file that exists outside ``scope``: the count comes
    with the advice to write the baseline again, and only then does that lose nothing.
    """
    if uncovered(unused, scope, root):
        return 0
    return sum(
        count
        for (path, rule, _), count in unused.items()
        if rule in rules and (path in checked or not _exists(root, path))
    )
