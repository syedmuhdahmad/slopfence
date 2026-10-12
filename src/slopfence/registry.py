"""PyPI lookups with an on-disk cache.

One lookup per package answers both package rules: whether the package exists
(SLOP001), and how long it has been on PyPI (SLOP002).

No answer never produces a finding. A network failure, a rate limit or a page that
is not PyPI's is "unknown": no rule reports it, it is not cached, and no more
requests start in that run.

Names can be looked up as one batch, a few requests at a time. The batch only
changes how fast the answers arrive: the request for a name is the same.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, NamedTuple, Protocol, runtime_checkable

from slopfence import __version__
from slopfence.files import replace_text
from slopfence.listing import PackageInfo, is_young, oldest_seen_upload, parse_listing

EXISTS_TTL = 7 * 24 * 3600
MISSING_TTL = 24 * 3600
# An answer that can lead to a SLOP002 finding is asked for again after a day, like a
# missing package: the number of releases and the status change.
FACTS_TTL = 24 * 3600
# PyPI requests in flight at once.
LOOKUP_WORKERS = 8
# Where the file listing of a package is: PyPI's Simple API.
LISTING_URL = "https://pypi.org/simple/{}/"
# The JSON form of the Simple API (PEP 691), which has upload times (PEP 700).
SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"
# The content type of every form of the Simple API starts like this.
SIMPLE_TYPES = "application/vnd.pypi.simple."
# The bytes of the (gzip) listing that the first request for a package asks for.
LISTING_LIMIT = 16 * 1024
# The most bytes that are read when the whole listing is needed.
FULL_LISTING_LIMIT = 512 * 1024
# The most text a listing may unpack to: a few kilobytes can unpack to gigabytes. What
# the JSON parser may make of that text is limited in ``listing.parse_listing``.
MAX_LISTING_TEXT = 8 * 1024 * 1024
# A body that is not read this many seconds after its request was sent is given up.
# ``timeout`` alone limits each step before the body: the connection, and each line
# of the headers. So a server that sends its headers a line at a time, each just
# within the timeout, is waited for longer, as it was in v0.2.
READ_DEADLINE = 10.0
# No time in a cache file is further from 0 than this (Unix seconds). CI pins entries
# with 9e12.
MAX_TIME = 1e18
# The cache key that holds the facts. It is never a package name, and v0.2 keeps a
# key that it doesn't know.
META_KEY = "_meta"

# A normalized name that PyPI can hold. Nothing else is ever put in a URL.
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# "bytes 0-16383/548418": which bytes of how many a ranged answer holds.
_CONTENT_RANGE = re.compile(r"bytes\s+0-(\d{1,18})/(\d{1,18})\s*$")


def normalize(name: str) -> str:
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def is_pypi_name(name: str) -> bool:
    """Whether PyPI can hold a package with this name, in any spelling of it.

    ASCII letters and digits, with ``-``, ``_`` or ``.`` between them. No registry
    is asked about another name (``_speedups``, ``café``): there is nothing to ask.
    """
    return _NAME.fullmatch(normalize(name)) is not None


class Lookup(NamedTuple):
    """What PyPI said about one package."""

    found: bool
    # What its file listing says, when the listing was read whole and understood.
    info: PackageInfo | None = None


class Registry(Protocol):
    """What the package rules need from a package registry."""

    def exists(self, name: str) -> bool | None:
        """True/False if known, None if the registry could not be reached."""


@runtime_checkable
class BatchRegistry(Registry, Protocol):
    """A registry that can also look up many names at once.

    This part is optional. A registry with only ``exists`` is asked one name at a time.
    """

    def prefetch(self, names: Iterable[str]) -> None:
        """Look up ``names`` now, so that ``exists`` can answer each without a request."""


@runtime_checkable
class FactsRegistry(Registry, Protocol):
    """A registry that also knows more about a package than whether it exists.

    This part is optional too. SLOP002 has nothing to go on without it, and reports
    nothing.
    """

    def info(self, name: str) -> PackageInfo | None:
        """What is known about a package that ``exists`` said yes to. None when nothing is.

        This never sends a request: it reads what the answer to ``exists`` held.
        """


class OfflineRegistry:
    """The registry of an ``--offline`` run: it knows nothing and asks nobody."""

    def exists(self, name: str) -> bool | None:
        """Always unknown."""
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keeps a request from going on to another address than the one it was sent to."""

    def redirect_request(self, *args: object) -> None:
        """Follow no redirect. urllib then raises the redirect as an ``HTTPError``."""
        return None


class _Listing(NamedTuple):
    """PyPI's answer to one request for the file listing of a package."""

    # False when PyPI has no such package (status 404).
    found: bool
    # The listing as far as it was read. None when it could not be read at all.
    text: bytes | None = None
    # Whether ``text`` is the whole listing.
    complete: bool = False


def _is_time(value: object) -> bool:
    """Whether a value of the cache file can be a time: a number the clock can be
    compared with.

    Not a whole number of hundreds of digits, which JSON can hold and no float
    arithmetic takes. ``type``, not ``isinstance``: True is an int too.
    """
    return type(value) is float or (type(value) is int and abs(value) < MAX_TIME)


def _is_entry(cached: object) -> bool:
    """Whether a value of the cache file is a well-formed ``[found, time]`` entry."""
    return (
        isinstance(cached, list)
        and len(cached) == 2
        and isinstance(cached[0], bool)
        and _is_time(cached[1])
    )


def _are_facts(stored: object) -> bool:
    """Whether a value under ``META_KEY`` is well-formed: ``[first upload, releases,
    status, time of the lookup]``."""
    if not (isinstance(stored, list) and len(stored) == 4):
        return False
    first, releases, status, checked = stored
    return (
        (first is None or (type(first) is int and _is_time(first)))
        and type(releases) is int
        and releases >= 0
        and (status is None or isinstance(status, str))
        and _is_time(checked)
    )


def _read_body(response: http.client.HTTPResponse, limit: int, deadline: float) -> bytes:
    """Up to ``limit`` + 1 bytes of the body of a response.

    The one byte more shows that the body is longer than ``limit``. Raises
    TimeoutError when the clock passes ``deadline`` (a ``time.monotonic`` value)
    before the body is read, and ``IncompleteRead`` when the connection ends before
    the bytes that ``Content-Length`` promised.
    """
    body = bytearray()
    while len(body) <= limit:
        if time.monotonic() > deadline:
            raise TimeoutError("PyPI answered too slowly")
        # read1 returns after one read from the connection, so the deadline is looked
        # at again at least once per timeout.
        part = response.read1(limit + 1 - len(body))
        if not part:
            # ``read1`` gives nothing at the end of a body, and also when the
            # connection ends early. Only ``length`` tells the two apart: the bytes
            # still to come, None when the server did not say.
            if response.length:
                raise http.client.IncompleteRead(bytes(body), response.length)
            break
        body += part
    return bytes(body)


def _unpacked(body: bytes, encoding: str, complete: bool) -> bytes | None:
    """The text of a body with the given ``Content-Encoding``. None when it can't be read.

    That is so for another encoding than gzip, for gzip that does not decode, and
    for a ``complete`` body that stops before its gzip stream ends or unpacks to more
    than ``MAX_LISTING_TEXT``. A body that is only the start of a listing gives the
    start of the text.
    """
    if encoding in ("", "identity"):
        return body
    if encoding != "gzip":
        return None
    unzip = zlib.decompressobj(wbits=31)
    try:
        text = unzip.decompress(body, MAX_LISTING_TEXT)
    except zlib.error:
        return None
    if complete and (unzip.unconsumed_tail or not unzip.eof):
        return None
    return text


def _read_listing(
    response: http.client.HTTPResponse, limit: int, deadline: float
) -> _Listing | None:
    """What a response to a request for a file listing says. None when it is not
    PyPI's answer: another status than 200 or 206, or a content type that is not the
    Simple API's (the page of a proxy or a Wi-Fi login).

    At most ``limit`` + 1 bytes of the body are read, until ``deadline`` at the latest
    (see ``_read_body``).
    """
    kind = response.headers.get_content_type()
    if response.status not in (200, 206) or not kind.startswith(SIMPLE_TYPES):
        return None
    if kind != SIMPLE_JSON:
        # PyPI answered in another form than the one asked for: the package exists.
        return _Listing(found=True)
    body = _read_body(response, limit, deadline)
    if response.status == 206:
        m = _CONTENT_RANGE.match(response.headers.get("Content-Range") or "")
        complete = m is not None and int(m.group(1)) + 1 == int(m.group(2))
    else:
        # The whole listing was asked for, or the server took no notice of ``Range``.
        complete = len(body) <= limit
    encoding = (response.headers.get("Content-Encoding") or "").strip().lower()
    return _Listing(True, _unpacked(body, encoding, complete), complete)


class PyPIRegistry:
    """PyPI over HTTPS, with the answers cached on disk between runs.

    The cache file maps each name to ``[found, time]``, as v0.2 wrote it. The facts
    SLOP002 uses are kept apart, under ``META_KEY``: ``{name: [first upload, releases,
    status, time of the lookup]}``, and only for a package that the rule could report
    (under a year old, or quarantined). So an entry that has facts is the kind that
    is asked for again after a day.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        timeout: float = 5.0,
        workers: int = LOOKUP_WORKERS,
        now: float | None = None,
    ) -> None:
        """``workers`` is how many requests a batch may have in flight at once.

        ``now`` is the time of the run (Unix seconds; default: the clock). Cached
        answers are fresh or old by it, new ones are stamped with it, and a package
        is young or not by it.
        """
        if cache_dir is None:
            base = os.environ.get("SLOPFENCE_CACHE_DIR") or os.environ.get("XDG_CACHE_HOME")
            cache_dir = Path(base) / "slopfence" if base else Path.home() / ".cache" / "slopfence"
        self.cache_file = cache_dir / "pypi.json"
        self.timeout = timeout
        self.workers = workers
        self._now = time.time() if now is None else now
        self._cache: dict[str, Any] = self._load()
        meta = self._cache.pop(META_KEY, None)
        self._meta: dict[str, Any] = meta if isinstance(meta, dict) else {}
        self._dirty = False
        self._unreachable = False
        # The names PyPI was already asked about by this object, which lives for one
        # run. No name is requested twice, also when there was no answer the first time.
        self._asked: set[str] = set()
        # Made for the first request: it loads the certificates of the system.
        self._opener: urllib.request.OpenerDirector | None = None
        self._opener_lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        """The content of the cache file. Empty if it can't be read or is no JSON object."""
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
        # RecursionError: a damaged file can be nested more deeply than the parser takes.
        except (OSError, ValueError, RecursionError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self) -> None:
        """Write the cache file, if anything changed. Called once, at the end of a run."""
        if not self._dirty:
            return
        data = dict(self._cache)
        if self._meta:
            data[META_KEY] = self._meta
        # A cache that can't be written only costs the next run its lookups.
        with contextlib.suppress(OSError):
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            # In one step, so that another slopfence process never reads half a file.
            replace_text(self.cache_file, json.dumps(data))
            self._dirty = False

    def exists(self, name: str) -> bool | None:
        """Whether PyPI has the package: from the cache if that is fresh, else one lookup.

        None when PyPI gave no answer. Asking again in the same run sends nothing more.
        """
        key = normalize(name)
        self._fetch([key])
        return self._known(key)

    def info(self, name: str) -> PackageInfo | None:
        """What PyPI's file listing says about a package, for SLOP002. None if nothing is known.

        This only reads what the lookups of ``exists`` and ``prefetch`` stored: it
        never sends a request. Nothing is known about a package that is missing, that
        has no fresh answer, or that is a year old or more. Nor about one whose
        answer v0.2 wrote, or that is pinned in the cache file without facts.
        """
        key = normalize(name)
        if self._known(key) is not True or not self._has_facts(key):
            return None
        first, releases, status, _ = self._meta[key]
        return PackageInfo(first, releases, status)

    def prefetch(self, names: Iterable[str]) -> None:
        """Look up many names at once, so that ``exists`` answers them from the cache."""
        self._fetch(dict.fromkeys(normalize(name) for name in names))

    def _has_facts(self, key: str) -> bool:
        """Whether well-formed facts are stored for a normalized name."""
        return _are_facts(self._meta.get(key))

    def _known(self, key: str) -> bool | None:
        """What is known about a normalized name without asking PyPI. None if nothing is.

        It is the cached answer, while that is fresh. Facts have a time of their own.
        So a name with facts is asked for again a day after they were read, also
        when a v0.2 run has rewritten its entry since. An entry without facts
        (written by v0.2, or pinned) is trusted as it is.
        """
        entry = self._cache.get(key)
        if not _is_entry(entry):
            return None
        found, stamp = entry
        if found and self._has_facts(key):
            fresh = self._now - self._meta[key][3] < FACTS_TTL
        else:
            fresh = self._now - stamp < (EXISTS_TTL if found else MISSING_TTL)
        return found if fresh else None

    def _store(self, key: str, answer: Lookup) -> None:
        """Record one answer. The facts of an earlier answer never outlive it.

        Call this on the thread that owns the cache, not on a worker thread.
        """
        self._cache[key] = [answer.found, self._now]
        info = answer.info if answer.found else None
        # Only what SLOP002 could report is kept: see the class docstring.
        if info is not None and (
            info.quarantined
            or (info.first_upload is not None and is_young(info.first_upload, self._now))
        ):
            self._meta[key] = [info.first_upload, info.releases, info.status, self._now]
        else:
            self._meta.pop(key, None)
        self._dirty = True

    def _fetch(self, keys: Iterable[str]) -> None:
        """Ask PyPI about the keys that nothing is known about, and cache what it says.

        An unknown answer (unreachable, rate limited, an unexpected status) is not cached,
        so the next run asks again. This run does not: each key is requested once. A
        key that PyPI can't hold as a name is never requested, so nothing is known
        about it: only PyPI's own answer says that a package is missing.
        """
        wanted = [
            key
            for key in keys
            if _NAME.fullmatch(key) and key not in self._asked and self._known(key) is None
        ]
        self._asked.update(wanted)
        for key, answer in zip(wanted, self._query_all(wanted), strict=True):
            if answer is not None:
                self._store(key, answer)

    def _query_all(self, keys: Sequence[str]) -> list[Lookup | None]:
        """``_query`` for every key, up to ``self.workers`` at a time; answers in key order.

        The answer is None for a key that was skipped because the registry had been
        found unreachable: no request starts after that. Requests already in flight
        finish on their own, and their answers still count.

        ``_query`` runs on worker threads here. It may set ``self._unreachable`` and
        must not touch the cache: the caller stores the answers, on its own thread.
        """

        def one(key: str) -> Lookup | None:
            """Query one key, unless the registry has been found unreachable."""
            return None if self._unreachable else self._query(key)

        if len(keys) < 2 or self.workers < 2:
            return [one(key) for key in keys]
        # Imported here: a run that looks nothing up (offline, warm cache) doesn't pay for it.
        from concurrent.futures import ThreadPoolExecutor

        pool = ThreadPoolExecutor(
            max_workers=min(self.workers, len(keys)), thread_name_prefix="slopfence-pypi"
        )
        try:
            return list(pool.map(one, keys))
        finally:
            # Nothing is left to wait for when every answer is in. After Ctrl-C there
            # is: each request in flight could take the whole timeout. They are left
            # behind, and the ones not sent yet are dropped.
            pool.shutdown(wait=False, cancel_futures=True)

    def _query(self, key: str) -> Lookup | None:
        """Ask PyPI about one package. None when it gave no answer.

        One request, for the start of the file listing. It is enough for a package
        whose listing fits in it, and for one with an old file in that part: SLOP002
        never reports a package that old. A second request, for the whole listing,
        is only sent when the listing is longer and no file seen is old: every one
        is young, or the part shows no upload time at all. Only a whole listing can
        show that a package is young (see ``listing.oldest_seen_upload``). The status
        and the versions come after the files, so they are only read from a whole
        listing too.

        This runs on worker threads: it reads ``self._now`` and changes nothing but
        ``self._unreachable``.
        """
        start = self._get(key, LISTING_LIMIT, ranged=True)
        if start is None:
            return None
        if not start.found or start.text is None:
            return Lookup(start.found)
        if start.complete:
            return Lookup(True, parse_listing(start.text))
        oldest = oldest_seen_upload(start.text)
        if oldest is not None and not is_young(oldest, self._now):
            return Lookup(True)
        if self._unreachable:
            # Another request found PyPI out of reach in the meantime. The package
            # exists, but calling it known would keep SLOP002 from it for a week.
            return None
        whole = self._get(key, FULL_LISTING_LIMIT, ranged=False)
        if whole is None:
            return None
        if whole.found and whole.text is not None and whole.complete:
            return Lookup(True, parse_listing(whole.text))
        # Gone since the first request, or too long even for this one.
        return Lookup(whole.found)

    def _get(self, key: str, limit: int, ranged: bool) -> _Listing | None:
        """One request for the file listing of a package, of which ``limit`` bytes are read.

        With ``ranged`` only those bytes are asked for. None when the answer is not
        PyPI saying yes or no: no connection, a timeout, a rate limit, an outage, a
        redirect, a page of a proxy. Then no more requests start in this run.
        """
        headers = {
            "User-Agent": f"slopfence/{__version__}",
            "Accept": SIMPLE_JSON,
            "Accept-Encoding": "gzip",
        }
        if ranged:
            headers["Range"] = f"bytes=0-{limit - 1}"
        request = urllib.request.Request(LISTING_URL.format(key), headers=headers)
        deadline = time.monotonic() + READ_DEADLINE
        try:
            with self._open(request) as response:
                answer = _read_listing(response, limit, deadline)
        except urllib.error.HTTPError as err:
            err.close()
            # Only a 404 says that PyPI has no such package. A rate limit (429), an
            # outage (5xx), a block (403) and a redirect say nothing about it.
            answer = _Listing(found=False) if err.code == 404 else None
        # OSError: no connection, a timeout, a certificate that is not PyPI's.
        # HTTPException: an answer that is not HTTP, or that breaks off.
        except (OSError, http.client.HTTPException):
            answer = None
        if answer is None:
            # Stop asking a registry that can't be reached, or that asked for a pause.
            self._unreachable = True
        return answer

    def _open(self, request: urllib.request.Request) -> http.client.HTTPResponse:
        """Send one request and return the response. Nothing else opens a connection.

        A redirect is not followed, so no request ever goes to another address than
        the one it was made for. urllib raises it as an ``HTTPError``.
        """
        with self._opener_lock:
            if self._opener is None:
                self._opener = urllib.request.build_opener(_NoRedirect)
            opener = self._opener
        return opener.open(request, timeout=self.timeout)
