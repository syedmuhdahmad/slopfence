"""SLOP002: packages that exist on PyPI but are too new to trust without a look.

SLOP001 stops reporting an invented package name the moment someone registers it.
What gives such a package away is that it is young: almost nothing that real
projects depend on was first uploaded in the last few weeks. A package is risky when

* PyPI has quarantined it, or
* its oldest file on PyPI is less than ``min-package-age`` days old (30), or
* it is less than a year old and has had one release: what a name looks like that
  was registered once and then left.

Only facts from PyPI's own file listing count (``listing.PackageInfo``), never the
name. When they aren't known, nothing is risky.

This module judges facts. Which packages are looked up, and where a finding goes, is
decided in ``packages``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import NamedTuple

from slopfence.listing import PackageInfo
from slopfence.models import NEW_PACKAGE_DAYS, YOUNG_PACKAGE_DAYS, plural

# 2003-01-01. PyPI is younger than that, so an earlier upload time is not a real one.
PYPI_OPENED = 1041379200
CHECK = "check its PyPI page and make sure it is the package you mean (slopsquatting risk)"

# The kinds of risk.
QUARANTINED, NEW, SINGLE_RELEASE = "quarantined", "new", "single-release"


class Risk(NamedTuple):
    """Why a package that exists on PyPI deserves a look."""

    kind: str
    # The end of a sentence that starts with the package: "is new on PyPI (...)".
    text: str

    def subject(self, name: str) -> str:
        """What a finding of this risk about the package ``name`` is about, for baselines.

        A quarantine has a subject of its own. A baseline entry that was made while
        the package was merely new must not hide "quarantined: remove it" later.
        """
        return f"{name} {QUARANTINED}" if self.kind == QUARANTINED else name


def _utc_day(stamp: float) -> date:
    """The UTC calendar day of a Unix time."""
    return datetime.fromtimestamp(stamp, timezone.utc).date()


def assess(info: PackageInfo | None, now: float, min_age: int = NEW_PACKAGE_DAYS) -> Risk | None:
    """The risk of a package that exists on PyPI. None when there is none to report.

    ``now`` is a Unix time, and ``min_age`` the days a package must have been on PyPI.
    Ages are whole UTC calendar days, and the text names the upload date, not the
    age. So a report reads the same all day, and only changes on the day the package
    stops being young.
    """
    if info is None:
        return None
    if info.quarantined:
        return Risk(
            QUARANTINED,
            "is quarantined by PyPI: remove it until PyPI lifts the quarantine",
        )
    if info.first_upload is None or not PYPI_OPENED < info.first_upload < now:
        # Not known, or a date that can't be right: a clock that is behind, a damaged cache.
        return None
    first = _utc_day(info.first_upload)
    days = (_utc_day(now) - first).days
    if days < min_age:
        # The count is 0 when the listing didn't say. Then it is left out.
        count = f", {plural(info.releases, 'release')}" if info.releases else ""
        return Risk(NEW, f"is new on PyPI (oldest upload {first}{count}): {CHECK}")
    if days < YOUNG_PACKAGE_DAYS and info.releases == 1:
        return Risk(SINGLE_RELEASE, f"has only one release on PyPI (uploaded {first}): {CHECK}")
    return None
