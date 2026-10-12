"""Reading PyPI's file listing (#39): untrusted text in, a few facts or nothing out."""

import json

import pytest

from slopfence import listing as listing_module
from slopfence.listing import (
    MAX_CONTAINERS,
    MAX_DEPTH,
    MAX_STRINGS,
    QUARANTINED,
    YOUNG_SECONDS,
    PackageInfo,
    is_young,
    oldest_seen_upload,
    parse_listing,
)
from tests.conftest import DAY, NOW


def listing(*uploads, versions=None, status="active"):
    """A Simple API JSON document with one file per upload time."""
    files = [
        {"filename": f"pkg-{n}.tar.gz", "size": 1000 + n, "upload-time": time}
        for n, time in enumerate(uploads)
    ]
    doc = {"files": files, "meta": {"api-version": "1.4"}, "name": "pkg"}
    doc["versions"] = [str(n) for n in range(len(files))] if versions is None else versions
    if status:
        doc["project-status"] = {"status": status}
    return json.dumps(doc).encode()


def test_listing_gives_oldest_upload_release_count_and_status():
    """The oldest upload of any file, wherever it is listed; the versions; PyPI's status."""
    doc = listing(
        "2026-10-03T01:02:11.752910Z", "2026-10-02T16:51:19.026723Z", "2026-10-02T16:51:20Z"
    )
    assert parse_listing(doc) == PackageInfo(1790959879, 3, "active")


def test_listing_without_files_says_nothing_unless_quarantined():
    """PIL has a project and no files: nothing is known. Only a quarantine is a fact then."""
    assert parse_listing(listing(versions=[], status="archived")) is None
    held = parse_listing(listing(status=QUARANTINED))
    assert held == PackageInfo(None, 0, "quarantined") and held.quarantined
    assert not PackageInfo(5, 1, "active").quarantined


@pytest.mark.parametrize(
    "text",
    [
        b"<html>captive portal</html>",
        b"",
        b"[]",
        b"null",
        b'"files"',
        b'{"files": "none"}',
        b'{"name": "pkg"}',
        b'{"files": [1, 2, 3]}',
        b'{"files": [{"upload-time": "2026-10-02T16:51:19Z"}, "odd"]}',
        b"\xff\xfe not text at all \x00",
    ],
)
def test_unexpected_listing_gives_no_facts(text):
    """Anything that isn't a listing means: nothing known. It is no error, and no "missing"."""
    assert parse_listing(text) is None


@pytest.mark.parametrize(
    "time",
    [
        None,
        20261002,
        "yesterday",
        "2026-10-02",
        "2026-10-02T16:51:19",  # no Z: not UTC for sure
        "2026-10-02T16:51:19+05:00",
        "2026-10-02T16:51:19.1234567Z",  # more digits than PEP 700 allows
        "2026-13-02T16:51:19Z",
        "2026-02-30T16:51:19Z",
        "2026-10-02T24:00:00Z",
        "2026-10-02T16:51:61Z",
        "0000-01-01T00:00:00Z",
        " 2026-10-02T16:51:19Z",
        "2026-10-02T16:51:19Z and more",
    ],
)
def test_a_file_without_a_readable_upload_time_makes_the_age_unknown(time):
    """One file without a time could be the oldest, so no age is given at all."""
    files = [{"upload-time": "2026-10-02T16:51:19Z"}, {"filename": "x", "upload-time": time}]
    doc = json.dumps({"files": files, "versions": ["1"]}).encode()
    assert parse_listing(doc) == PackageInfo(None, 1, None)


def test_release_count_is_zero_when_the_listing_does_not_say():
    """No ``versions`` key, or one of another type: the count is unknown, which is 0."""
    files = [{"upload-time": "2026-10-02T16:51:19Z"}]
    for versions in ({}, {"versions": "many"}, {"versions": {"1": 1}}):
        assert parse_listing(json.dumps({"files": files, **versions}).encode()).releases == 0


@pytest.mark.parametrize("marker", ["quarantined", ["quarantined"], {"status": 5}, {}, None])
def test_status_of_another_shape_is_no_status(marker):
    """Only ``{"status": "..."}`` is read."""
    doc = {"files": [{"upload-time": "2026-10-02T16:51:19Z"}], "project-status": marker}
    assert parse_listing(json.dumps(doc).encode()).status is None


def test_only_the_statuses_pypi_defines_are_kept():
    """No other text of a listing is ever stored or shown."""
    for status in ("active", "archived", "deprecated", "quarantined"):
        assert parse_listing(listing("2026-10-02T16:51:19Z", status=status)).status == status
    for status in ("Quarantined", "held", "x" * 5000, ""):
        assert parse_listing(listing("2026-10-02T16:51:19Z", status=status)).status is None
    # A project without files is only a fact when the status is exactly PyPI's word.
    assert parse_listing(listing(status="QUARANTINED")) is None


def test_numbers_in_a_listing_are_never_converted():
    """A number with a million digits is skipped, not converted (minutes on old Pythons)."""
    huge = "9" * 1_000_000
    file = '{"size": HUGE, "ratio": 1.HUGE, "upload-time": "2026-10-02T16:51:19Z"}'
    doc = ('{"files": [' + file + '], "versions": [HUGE]}').replace("HUGE", huge)
    assert parse_listing(doc.encode()) == PackageInfo(1790959879, 1, None)


def flood(item, count):
    """A listing with one real file, and ``count`` times ``item`` in a key of its own."""
    file = '{"filename": "pkg-1.whl", "upload-time": "2026-10-02T16:51:19Z"}'
    more = ",".join([item] * count)
    return f'{{"files": [{file}], "versions": ["1"], "more": [{more}]}}'.encode()


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(b"[" * 100_000, id="nested too deeply"),
        pytest.param(b'{"files": ' + b"[" * 100_000, id="files nested too deeply"),
        pytest.param(b'{"a":' * 100_000, id="objects nested too deeply"),
        pytest.param(b"[" * (MAX_DEPTH + 1) + b"]" * (MAX_DEPTH + 1), id="one level too deep"),
        pytest.param(flood("{}", MAX_CONTAINERS), id="too many objects"),
        pytest.param(flood("[]", MAX_CONTAINERS), id="too many lists"),
        pytest.param(flood('"aa"', MAX_STRINGS), id="too many strings"),
    ],
)
def test_a_document_that_is_too_much_for_the_parser_never_reaches_it(monkeypatch, text):
    """The JSON parser needs memory for every object, list and string (a hundred times
    the text for ``{}``), and stack for every level (enough to end Python 3.12 on a
    thread with a small stack). Such a document is turned down before it is parsed."""
    monkeypatch.setattr(
        listing_module.json, "loads", lambda *args, **kwargs: pytest.fail("the parser was called")
    )
    assert parse_listing(text) is None


def test_text_in_another_encoding_is_not_decoded():
    """PyPI's listings are UTF-8. The JSON parser would work out that a text is UTF-16
    and decode it, and then read other brackets than the ones that were counted: here
    100,000 levels that the count takes for the inside of a string."""
    hidden = '["\\"", ' + "[" * 100_000 + '"'
    assert parse_listing(hidden.encode("utf-16-le")) is None
    assert parse_listing(hidden.encode("utf-16")) is None
    assert parse_listing(listing("2026-10-02T16:51:19Z").decode().encode("utf-16-le")) is None
    assert parse_listing("\ufeff".encode() + listing("2026-10-02T16:51:19Z")) is None


def test_a_listing_within_the_bounds_is_parsed():
    """The bounds are on what the parser builds, not on what a string holds: brackets
    and escaped quotes in a file name or a yank reason count for nothing."""
    assert parse_listing(flood("{}", 1000)) == PackageInfo(1790959879, 1, None)
    assert parse_listing(flood('"aa"', 1000)) == PackageInfo(1790959879, 1, None)
    deep = b"[" * (MAX_DEPTH - 2) + b"]" * (MAX_DEPTH - 2)
    assert parse_listing(flood(deep.decode(), 5)) == PackageInfo(1790959879, 1, None)
    reason = '[[{{ \\" [ \\\\' * (MAX_CONTAINERS // 2) + "[" * 3 * MAX_CONTAINERS
    files = [{"filename": "a.whl", "yanked": reason, "upload-time": "2026-10-02T16:51:19Z"}]
    doc = json.dumps({"files": files, "versions": ["1"], "project-status": {"status": "active"}})
    assert parse_listing(doc.encode()) == PackageInfo(1790959879, 1, "active")


def test_oldest_upload_time_is_found_in_a_listing_that_was_cut_off():
    """The start of a long listing is not valid JSON, but its upload times can be read."""
    text = listing("2026-10-02T16:51:19Z", "2014-11-11T20:30:40.123Z", "2019-01-01T00:00:00Z")[:-25]
    assert parse_listing(text) is None
    assert oldest_seen_upload(text) == 1415737840
    # The last time may be cut off itself: whatever is whole up to the second counts.
    assert oldest_seen_upload(b'{"files": [{"upload-time": "2014-11-11T20:30:40.1') == 1415737840
    assert oldest_seen_upload(b'{"files": [{"upload-time": "2014-11-11T20:30') is None
    assert oldest_seen_upload(b'{"files": [{"filename": "x"}, {"filename": "y"}') is None
    assert oldest_seen_upload(b"") is None


def test_a_time_that_is_no_real_date_is_passed_over():
    """Month 13 sorts before a real date and is no date: the oldest real one counts."""
    text = (
        b'{"files": [{"upload-time": "2013-13-01T00:00:00Z"}, '
        b'{"upload-time": "2020-02-30T00:00:00Z"}, {"upload-time": "2021-05-06T07:08:09Z"}, '
        b'{"upload-time": "2024-01-01T00:00:00Z"}'
    )
    assert oldest_seen_upload(text) == 1620284889
    assert oldest_seen_upload(b'{"files": [{"upload-time":"2014-13-11T20:30:40Z"}]}') is None


def test_a_file_name_cannot_pass_for_an_upload_time():
    """Text that the uploader chose can't make a new package look old: inside a JSON
    string a quote is escaped, so the key never appears in a value."""
    fake = '"upload-time": "1999-01-01T00:00:00Z"'
    files = [{"filename": fake, "yanked": fake, "upload-time": "2026-10-02T16:51:19Z"}]
    for ensure_ascii in (True, False):
        text = json.dumps({"files": files}, ensure_ascii=ensure_ascii).encode()
        assert oldest_seen_upload(text) == 1790959879


def test_young_is_under_a_year():
    """365 days: what SLOP002 never looks beyond."""
    assert YOUNG_SECONDS == 365 * DAY
    assert is_young(NOW - 365 * DAY + 1, NOW)
    assert not is_young(NOW - 365 * DAY, NOW)
    assert is_young(NOW + 5 * DAY, NOW)  # a clock that is behind: the rule sorts that out
