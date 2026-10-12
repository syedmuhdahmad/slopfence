"""What PyPI's file listing says about a package (SLOP002).

The listing is the JSON form of PyPI's Simple API (PEP 691). It has an upload time
for every file (PEP 700) and a status for the project (PEP 792).

It comes from the network, so nothing in it is trusted. Whatever is not as expected
means "nothing is known": it never becomes a fact, and it never ends the run. No text
of a listing is kept but a status that PEP 792 names.

Nothing here opens a connection or reads a file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from slopfence.models import YOUNG_PACKAGE_DAYS

# The project status of a package that PyPI has quarantined (PEP 792).
QUARANTINED = "quarantined"
# Every status PEP 792 names. Any other value in a listing is read as no status.
STATUSES = frozenset({"active", "archived", "deprecated", QUARANTINED})
YOUNG_SECONDS = YOUNG_PACKAGE_DAYS * 86400

# What a listing may hold before the JSON parser sees it. The parser builds an object
# for every list, object and string of a text, and goes one level down the stack for
# every level of nesting. So a text of 8 MB can cost 200 MB (``{}`` over and over), and
# one of 10 KB can end Python 3.12 on a thread with a small stack (``[`` over and
# over). A real listing stays far below these bounds: the longest one that was
# measured (tf-nightly, 2,272 files in 1.5 MB) has 9,093 lists and objects and 45,621
# strings, nested 4 deep.
MAX_CONTAINERS = 100_000
MAX_STRINGS = 500_000
MAX_DEPTH = 16

_OPENERS = b"[{"
# Every byte but the quote and the four brackets.
_NO_MARKS = bytes(set(range(256)) - set(b'"]}' + _OPENERS))

# An upload time as PEP 700 writes it: UTC, with or without a fraction of a second.
# ``datetime.fromisoformat`` only takes the "Z" from Python 3.11 on.
_UPLOAD_TIME = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d{1,6})?Z")
# The same, down to the second, in a listing that was cut off: the last time may be
# cut off too. A file name can't pass for one: inside a JSON string a quote is
# written as \".
_UPLOAD_TIMES = re.compile(rb'"upload-time":\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)')


@dataclass(frozen=True)
class PackageInfo:
    """What PyPI's file listing says about a package that exists."""

    # Unix time of the oldest file on PyPI. None when a file has no upload time that
    # can be read: that file could be the oldest.
    first_upload: int | None
    # The number of versions. 0 when the listing doesn't say.
    releases: int
    # The PEP 792 status ("active", "archived", "deprecated", "quarantined"), if any.
    status: str | None = None

    @property
    def quarantined(self) -> bool:
        """Whether PyPI has quarantined the package."""
        return self.status == QUARANTINED


def is_young(uploaded: float, now: float) -> bool:
    """Whether something uploaded at ``uploaded`` is under a year old at ``now`` (Unix times).

    SLOP002 never reports a package whose oldest file is not young, unless it is
    quarantined (see ``models.YOUNG_PACKAGE_DAYS``).
    """
    return now - uploaded < YOUNG_SECONDS


def _seconds(parts: tuple[str, ...]) -> int | None:
    """The Unix time of (year, month, day, hour, minute, second) in UTC.

    None for a date or time that doesn't exist, such as month 13 or second 61.
    """
    try:
        moment = datetime(*(int(part) for part in parts), tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(moment.timestamp())


def _timestamp(value: object) -> int | None:
    """The Unix time of a PEP 700 upload time such as ``2026-10-02T16:51:19.026723Z``."""
    m = _UPLOAD_TIME.fullmatch(value) if isinstance(value, str) else None
    return _seconds(m.groups()) if m else None


def oldest_seen_upload(text: bytes) -> int | None:
    """The oldest upload time in the start of a listing that was cut off, so is not
    valid JSON. None when that part shows no upload time.

    It can show that a package is old: one old file is enough. It can't show that a
    package is young, because an older file may follow in the part that is missing.
    """
    # Times written this way sort like the moments they name. So only the first one
    # that is a real date has to be converted, not each of the hundred or so.
    seen = sorted(set(_UPLOAD_TIMES.findall(text)))
    times = (_timestamp(stamp.decode("ascii") + "Z") for stamp in seen)
    return next((time for time in times if time is not None), None)


def _unread(number: str) -> None:
    """Stand in for every number of a listing: none is used.

    Python before 3.10.7 takes minutes to convert a number with millions of digits.
    """
    return None


def _is_bounded(text: bytes) -> bool:
    """Whether a text is safe to hand to the JSON parser: it holds no more lists,
    objects and strings than ``MAX_CONTAINERS`` and ``MAX_STRINGS``, and nothing is
    nested more deeply than ``MAX_DEPTH``.

    The counts are right for valid JSON, and for the valid start of any other text,
    which is as far as the parser reads. Brackets inside strings are not counted.
    """
    # Without the escaped backslashes and quotes, every quote left starts or ends a
    # string. Then only the quotes and the brackets matter.
    marks = text.replace(b"\\\\", b"").replace(b'\\"', b"").translate(None, _NO_MARKS)
    if marks.count(b'"') > 2 * MAX_STRINGS:
        return False
    # Every second part between two quotes is outside the strings.
    brackets = b"".join(marks.split(b'"')[::2])
    if len(brackets) > 2 * MAX_CONTAINERS:
        return False
    depth = 0
    for bracket in brackets:
        depth += 1 if bracket in _OPENERS else -1
        if depth > MAX_DEPTH:
            return False
    return True


def parse_listing(text: bytes) -> PackageInfo | None:
    """The facts in a complete listing. None when there are none to read.

    That is so for anything unexpected: text that is not UTF-8 or not JSON, JSON of
    another shape, JSON with more in it than a listing has (see ``_is_bounded``). It
    is also so for a project without files, which old projects such as PIL are: only
    a quarantined project gives a fact without a file. None never means that the
    package is missing.
    """
    if not _is_bounded(text):
        return None
    try:
        # Decoded here: given bytes, the parser would guess the encoding, and read
        # UTF-16 text as other JSON than the one that was just measured.
        doc = json.loads(text.decode("utf-8"), parse_int=_unread, parse_float=_unread)
    except ValueError:  # also what text that is not UTF-8 raises
        return None
    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(files, list) or not all(isinstance(file, dict) for file in files):
        return None
    marker = doc.get("project-status")
    status = marker.get("status") if isinstance(marker, dict) else None
    status = status if isinstance(status, str) and status in STATUSES else None
    if not files:
        return PackageInfo(None, 0, status) if status == QUARANTINED else None
    times = [_timestamp(file.get("upload-time")) for file in files]
    versions = doc.get("versions")
    return PackageInfo(
        first_upload=None if None in times else min(times),
        releases=len(versions) if isinstance(versions, list) else 0,
        status=status,
    )
