"""PyPI existence lookups with an on-disk cache.

Network failures never produce findings: an unknown answer is treated as
"exists", because a false alarm is worse than a missed one.

Names can be looked up as one batch, a few requests at a time. The batch only
changes how fast the answers arrive: the request for a name is the same.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from slopfence import __version__
from slopfence.files import replace_text

EXISTS_TTL = 7 * 24 * 3600
MISSING_TTL = 24 * 3600
# PyPI requests in flight at once.
LOOKUP_WORKERS = 8


def normalize(name: str) -> str:
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


class Registry(Protocol):
    """What SLOP001 needs from a package registry."""

    def exists(self, name: str) -> bool | None:
        """True/False if known, None if the registry could not be reached."""


@runtime_checkable
class BatchRegistry(Registry, Protocol):
    """A registry that can also look up many names at once.

    This part is optional. A registry with only ``exists`` is asked one name at a time.
    """

    def prefetch(self, names: Iterable[str]) -> None:
        """Look up ``names`` now, so that ``exists`` can answer each without a request."""


class OfflineRegistry:
    """The registry of an ``--offline`` run: it knows nothing and asks nobody."""

    def exists(self, name: str) -> bool | None:
        """Always unknown."""
        return None


class PyPIRegistry:
    """PyPI over HTTPS, with the answers cached on disk between runs."""

    def __init__(
        self, cache_dir: Path | None = None, timeout: float = 5.0, workers: int = LOOKUP_WORKERS
    ) -> None:
        """``workers`` is how many requests a batch may have in flight at once."""
        if cache_dir is None:
            base = os.environ.get("SLOPFENCE_CACHE_DIR") or os.environ.get("XDG_CACHE_HOME")
            cache_dir = Path(base) / "slopfence" if base else Path.home() / ".cache" / "slopfence"
        self.cache_file = cache_dir / "pypi.json"
        self.timeout = timeout
        self.workers = workers
        self._cache: dict[str, list] = self._load()
        self._dirty = False
        self._unreachable = False
        # The names PyPI was already asked about by this object, which lives for one
        # run. No name is requested twice, also when there was no answer the first time.
        self._asked: set[str] = set()

    def _load(self) -> dict[str, list]:
        """The cache file's content: ``{name: [found, timestamp]}``. Empty if unreadable."""
        try:
            return json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        """Write the cache file, if anything changed. Called once, at the end of a run."""
        if not self._dirty:
            return
        # A cache that can't be written only costs the next run its lookups.
        with contextlib.suppress(OSError):
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            # In one step, so that another slopfence process never reads half a file.
            replace_text(self.cache_file, json.dumps(self._cache))
            self._dirty = False

    def exists(self, name: str) -> bool | None:
        """Whether PyPI has the package: from the cache if that is fresh, else one request.

        None when PyPI gave no answer. Asking again in the same run sends nothing more.
        """
        key, now = normalize(name), time.time()
        self._fetch([key], now)
        return self._cached(key, now)

    def prefetch(self, names: Iterable[str]) -> None:
        """Look up many names at once, so that ``exists`` answers them from the cache."""
        self._fetch(dict.fromkeys(normalize(name) for name in names), time.time())

    def _cached(self, key: str, now: float) -> bool | None:
        """The cached answer for a normalized name, or None if there is no fresh one."""
        cached = self._cache.get(key)
        if cached:
            found, stamp = cached
            if now - stamp < (EXISTS_TTL if found else MISSING_TTL):
                return found
        return None

    def _fetch(self, keys: Iterable[str], now: float) -> None:
        """Ask PyPI about the keys without a fresh cached answer, and cache what it says.

        An unknown answer (unreachable, rate limited, an unexpected status) is not cached,
        so the next run asks again. This run does not: each key is requested once.
        """
        wanted = [key for key in keys if key not in self._asked and self._cached(key, now) is None]
        self._asked.update(wanted)
        for key, found in zip(wanted, self._query_all(wanted), strict=True):
            if found is not None:
                self._cache[key] = [found, now]
                self._dirty = True

    def _query_all(self, keys: Sequence[str]) -> list[bool | None]:
        """``_query`` for every key, up to ``self.workers`` at a time; answers in key order.

        The answer is None for a key that was skipped because the registry had been
        found unreachable: no request starts after that. Requests already in flight
        finish on their own, and their answers still count.

        ``_query`` runs on worker threads here. It may set ``self._unreachable`` and
        must not touch the cache: the caller stores the answers, on its own thread.
        """

        def one(key: str) -> bool | None:
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

    def _query(self, key: str) -> bool | None:
        """One request to PyPI: True or False if it answers, None if the answer is unknown."""
        req = urllib.request.Request(
            f"https://pypi.org/pypi/{key}/json",
            method="HEAD",
            headers={"User-Agent": f"slopfence/{__version__}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return 200 <= resp.status < 400
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return False
            return None
        except (urllib.error.URLError, TimeoutError, OSError):
            # Stop hammering a registry we cannot reach for the rest of the run.
            self._unreachable = True
            return None
