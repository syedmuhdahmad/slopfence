"""What slopfence asks PyPI and what each answer means (#39).

One bounded request per package, a second one in rare cases, and a cache file that
v0.2 can still read. No test opens a socket: ``PyPIRegistry._open``, the one place
that does, is replaced by a fake that hands out answers shaped like pypi.org's.
"""

import email.message
import gzip
import hashlib
import http.client
import io
import json
import ssl
import time
import urllib.error
import urllib.request

import pytest

from slopfence import registry
from slopfence.engine import run
from slopfence.listing import PackageInfo
from slopfence.registry import (
    EXISTS_TTL,
    FACTS_TTL,
    FULL_LISTING_LIMIT,
    LISTING_LIMIT,
    MISSING_TTL,
    SIMPLE_JSON,
    FactsRegistry,
    Lookup,
    OfflineRegistry,
    PyPIRegistry,
)
from tests.conftest import DAY, NOW, DownRegistry, FakeFactsRegistry, FakeRegistry
from tests.test_listing import listing


def ago(days):
    """An upload time ``days`` days before ``NOW``, as PyPI writes it."""
    return time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(NOW - days * DAY))


class FakeResponse:
    """What ``_open`` returns: a status, headers and a body that arrives in parts."""

    def __init__(self, body=b"", status=200, content_type=SIMPLE_JSON, **headers):
        """``headers`` use ``_`` for ``-``: ``Content_Range="bytes 0-9/10"``."""
        self.status = status
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type
        for name, value in headers.items():
            self.headers[name.replace("_", "-")] = value
        self._body = body
        self._at = 0
        self.reads = 0
        # As in ``http.client``: the bytes of ``Content-Length`` that are still to come.
        # None when the server did not say how long the body is.
        promised = self.headers["Content-Length"]
        self.length = None if promised is None else int(promised)

    def read1(self, amount=-1):
        """The next part of the body: at most ``amount`` bytes, and never more than 1000."""
        self.reads += 1
        size = 1000 if amount < 0 else min(amount, 1000)
        part = self._body[self._at : self._at + size]
        self._at += len(part)
        if self.length is not None:
            self.length -= len(part)
        return part

    def __enter__(self):
        """Used as a context manager, like the real response."""
        self._at = 0
        return self

    def __exit__(self, *exc):
        """Nothing to close."""
        return False


def zipped(doc, status=200, **headers):
    """The whole of ``doc`` as a gzip answer."""
    return FakeResponse(gzip.compress(doc), status, Content_Encoding="gzip", **headers)


def partial(doc, total=None):
    """A 206 answer: the first 16 KB of ``doc`` (gzip), as pypi.org sends it."""
    body = gzip.compress(doc)
    sent = body[:LISTING_LIMIT]
    return FakeResponse(
        sent,
        206,
        Content_Encoding="gzip",
        Content_Range=f"bytes 0-{len(sent) - 1}/{total or len(body)}",
    )


def long_listing(first_days, files=400):
    """A listing too long for 16 KB whose files start ``first_days`` days ago."""
    doc = {
        "files": [
            {
                "filename": f"pkg-{n}-{'x' * 40}{n * 7919 % 1009}.whl",
                "hashes": {"sha256": hashlib.sha256(str(n).encode()).hexdigest()},
                "upload-time": ago(first_days - n * first_days / files),
            }
            for n in range(files)
        ],
        "versions": [str(n) for n in range(files // 2)],
        "project-status": {"status": "active"},
    }
    body = json.dumps(doc).encode()
    assert len(gzip.compress(body)) > LISTING_LIMIT
    return body


def http_error(code, name="x"):
    """The exception urllib raises for an HTTP error status."""
    url = f"https://pypi.org/simple/{name}/"
    return urllib.error.HTTPError(url, code, "error", email.message.Message(), io.BytesIO())


def fake_network(monkeypatch):
    """Replace the network: requests are answered from ``answers[name]`` and recorded.

    An answer is a response, an exception to raise, or a list of those to give in turn.
    """
    answers: dict[str, object] = {}
    requests: list[urllib.request.Request] = []

    def open_(self, request):
        """Answer from ``answers`` and record the request."""
        requests.append(request)
        answer = answers[request.full_url.rstrip("/").rsplit("/", 1)[-1]]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(PyPIRegistry, "_open", open_)
    return answers, requests


@pytest.fixture
def pypi(monkeypatch, tmp_path):
    """A registry with an empty cache and a fake network: (registry, answers, requests)."""
    answers, requests = fake_network(monkeypatch)
    return PyPIRegistry(tmp_path, now=NOW), answers, requests


def names(requests):
    """The package each request was for."""
    return [request.full_url.rstrip("/").rsplit("/", 1)[-1] for request in requests]


# --- the request ----------------------------------------------------------------------


def test_request_asks_for_the_start_of_the_simple_json_listing(pypi):
    """One GET to pypi.org's Simple API, limited to the first 16 KB, with nothing else in it."""
    reg, answers, requests = pypi
    answers["some-pkg"] = partial(listing(ago(5), ago(3)))
    assert reg.exists("Some_Pkg") is True
    assert reg.info("some.pkg") == PackageInfo(NOW - 5 * DAY, 2, "active")
    [request] = requests
    assert request.full_url == "https://pypi.org/simple/some-pkg/"
    assert (request.type, request.host) == ("https", "pypi.org")
    assert request.get_method() == "GET" and request.data is None
    assert dict(request.header_items()) == {
        "User-agent": f"slopfence/{registry.__version__}",
        "Accept": SIMPLE_JSON,
        "Accept-encoding": "gzip",
        "Range": f"bytes=0-{LISTING_LIMIT - 1}",
    }
    assert LISTING_LIMIT == 16 * 1024


@pytest.mark.parametrize(
    "name", ["café", "_private", "trailing_", "a/b", "../etc", "x?y=1", "a b", "", "naïve-pkg"]
)
def test_a_name_that_pypi_cannot_hold_is_never_requested(pypi, name):
    """No text that is no name is put in a URL. PyPI was not asked, so nothing is known:
    only its own 404 says "missing"."""
    reg, _, requests = pypi
    assert reg.exists(name) is None
    reg.prefetch([name, name])
    assert requests == [] and reg.info(name) is None
    reg.save()
    assert not reg.cache_file.exists()  # nothing was learned from PyPI


def test_import_that_is_no_pypi_name_is_not_asked_about(write, tmp_path, pypi, not_installed):
    """``import café`` used to end the run with a traceback: urllib can't send the name.
    No registry is asked about a name PyPI can't hold, so none can call it missing:
    not PyPI out of reach, not a registry that knows nothing, not a run that is offline."""
    reg, answers, requests = pypi
    answers["ghost-zz"] = answers["ghost_zz"] = urllib.error.URLError("no network")
    write("app.py", "import café\nimport _hidden_ext_zz\nimport trailing_\nimport ghost_zz\n")
    result = run([tmp_path], tmp_path, reg, select=["SLOP001"], check_imports=True)
    assert result.findings == [] and names(requests) == ["ghost-zz"]
    assert result.unchecked_rules == ["SLOP001"]  # because of ghost_zz, and of nothing else

    write("app.py", "import café\nimport _hidden_ext_zz\nimport trailing_\n")
    for other in (FakeRegistry(set()), DownRegistry(), OfflineRegistry()):
        result = run([tmp_path], tmp_path, other, select=["SLOP001"], check_imports=True)
        assert result.findings == [] and result.unchecked_rules == []
        assert getattr(other, "queries", []) == []


def test_redirects_are_not_followed(tmp_path, monkeypatch):
    """A request never goes on to another address: the opener turns a redirect into an error."""
    opened = []

    def open_(self, request, timeout=None):
        """Stand in for the network, and record which opener was used."""
        opened.append((self, timeout))
        return zipped(listing(ago(900)))

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_)
    reg = PyPIRegistry(tmp_path, timeout=3.5, now=NOW)
    assert reg.exists("one") is True and reg.exists("two") is True
    (opener, timeout), (again, _) = opened
    assert opener is again and timeout == 3.5  # one opener for the run
    redirectors = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert [type(h) for h in redirectors] == [registry._NoRedirect]
    # What urllib does when a 302 comes in. With its own handler it would open the new address.
    request = urllib.request.Request("https://pypi.org/simple/one/")
    headers = email.message.Message()
    headers["Location"] = "https://elsewhere.example/simple/one/"
    with pytest.raises(urllib.error.HTTPError) as raised:
        opener.error("http", request, io.BytesIO(), 302, "Found", headers)
    assert raised.value.code == 302 and len(opened) == 2


# --- what each answer means -------------------------------------------------------------


def test_facts_are_only_kept_for_packages_slop002_may_report(pypi):
    """A package a year old or more exists and that is all: no facts are stored."""
    reg, answers, _ = pypi
    answers["young"] = partial(listing(ago(364.9)))
    answers["old"] = partial(listing(ago(365), ago(2)))
    answers["held"] = partial(listing(status="quarantined"))
    answers["old-and-held"] = partial(listing(ago(3000), status="quarantined"))
    answers["no-times"] = partial(json.dumps({"files": [{"filename": "x"}]}).encode())
    assert reg.exists("young") and reg.info("young").first_upload == int(NOW - 364.9 * DAY)
    assert reg.exists("old") and reg.info("old") is None
    assert reg.exists("held") and reg.info("held") == PackageInfo(None, 0, "quarantined")
    assert reg.exists("old-and-held") and reg.info("old-and-held").quarantined
    assert reg.exists("no-times") and reg.info("no-times") is None
    assert set(reg._meta) == {"young", "held", "old-and-held"}


def test_long_listing_with_an_old_file_needs_one_request(pypi):
    """boto3's listing is 500 KB. A file from years ago in the first 16 KB settles it."""
    reg, answers, requests = pypi
    answers["boto3"] = partial(long_listing(4000), total=548418)
    assert reg.exists("boto3") is True
    assert reg.info("boto3") is None
    assert len(requests) == 1


def test_long_listing_with_only_young_files_is_read_whole(pypi):
    """Hundreds of files in a few weeks: a second request reads the whole listing."""
    reg, answers, requests = pypi
    doc = long_listing(20)
    answers["busy-new"] = [partial(doc), zipped(doc)]
    assert reg.exists("busy-new") is True
    assert reg.info("busy-new") == PackageInfo(NOW - 20 * DAY, 200, "active")
    assert [r.get_header("Range") for r in requests] == [f"bytes=0-{LISTING_LIMIT - 1}", None]
    assert [r.full_url for r in requests] == ["https://pypi.org/simple/busy-new/"] * 2
    assert requests[1].get_header("Accept") == SIMPLE_JSON and requests[1].data is None


def test_whole_listing_can_hold_an_old_file_further_down(pypi):
    """The first 16 KB showed young files only, but the package is years old."""
    reg, answers, requests = pypi
    young_part = long_listing(20)
    whole = json.loads(young_part)
    whole["files"].append({"filename": "pkg-0.1.tar.gz", "upload-time": ago(2000)})
    answers["pkg"] = [partial(young_part), zipped(json.dumps(whole).encode())]
    assert reg.exists("pkg") is True
    assert reg.info("pkg") is None
    assert len(requests) == 2


def test_second_request_that_gives_no_facts(pypi, monkeypatch):
    """Too long even for the second request, gone in between, or no answer."""
    reg, answers, requests = pypi
    doc = long_listing(20)
    monkeypatch.setattr(registry, "FULL_LISTING_LIMIT", 20000)
    answers["huge"] = [partial(doc), FakeResponse(b"x" * 30000)]
    assert reg.exists("huge") is True and reg.info("huge") is None
    assert answers["huge"] == [] and len(requests) == 2
    answers["gone"] = [partial(doc), http_error(404)]
    assert reg.exists("gone") is False
    answers["flaky"] = [partial(doc), http_error(503), partial(listing(ago(3)))]
    assert reg.exists("flaky") is None  # not cached, and nothing more is asked in this run
    assert reg.exists("later") is None
    assert names(requests) == ["huge", "huge", "gone", "gone", "flaky", "flaky"]
    assert "flaky" not in reg._cache and FULL_LISTING_LIMIT == 512 * 1024


def test_whole_listing_is_read_up_to_its_limit_and_no_further(pypi, monkeypatch):
    """The second request reads one byte more than the limit, to see that there is more."""
    reg, answers, _ = pypi
    monkeypatch.setattr(registry, "FULL_LISTING_LIMIT", 20000)
    endless = FakeResponse(b"x" * 10_000_000)
    answers["huge"] = [partial(long_listing(20)), endless]
    assert reg.exists("huge") is True
    assert endless._at == 20001


def test_no_second_request_once_pypi_is_out_of_reach(pypi):
    """Another request failed while this one was read: the whole listing is not asked for."""
    reg, answers, requests = pypi

    class Meanwhile(FakeResponse):
        """A first answer during which another thread finds PyPI out of reach."""

        def __exit__(self, *exc):
            """Do what a failed request on another thread does."""
            reg._unreachable = True
            return False

    first = partial(long_listing(20))
    answers["busy-new"] = Meanwhile(first._body, 206, **dict(first.headers.items()[1:]))
    assert reg.exists("busy-new") is None
    assert len(requests) == 1 and "busy-new" not in reg._cache


def test_server_that_ignores_range_is_read_up_to_the_limit(pypi):
    """A plain 200 answer: complete if it ends within the limit, cut off otherwise. A
    cut-off one is the start of a listing, like a ranged answer."""
    reg, answers, requests = pypi
    answers["small"] = FakeResponse(listing(ago(4)))
    doc = long_listing(20)
    start = FakeResponse(doc)  # the whole listing, although 16 KB were asked for
    answers["big"] = [start, zipped(doc)]
    assert reg.exists("small") is True and reg.info("small").releases == 1
    assert len(requests) == 1
    assert reg.exists("big") is True
    assert start._at == LISTING_LIMIT + 1 < len(doc)
    # Every file in the part that was read is young, so the whole listing is asked for.
    assert [r.get_header("Range") for r in requests[1:]] == [f"bytes=0-{LISTING_LIMIT - 1}", None]
    assert reg.info("big") == PackageInfo(NOW - 20 * DAY, 200, "active")


def test_info_never_sends_a_request(pypi):
    """``info`` reads what a lookup stored. Before the lookup there is nothing to read."""
    reg, answers, requests = pypi
    answers["new-pkg"] = partial(listing(ago(4)))
    assert reg.info("new-pkg") is None and requests == []
    assert reg.exists("new-pkg") is True
    assert reg.info("new-pkg") is not None and reg.info("New_Pkg") is not None
    assert len(requests) == 1


def test_missing_package(pypi):
    """Only a 404 means the package doesn't exist."""
    reg, answers, _ = pypi
    answers["ghost"] = http_error(404, "ghost")
    assert reg.exists("ghost") is False
    assert reg.info("ghost") is None
    assert reg._cache == {"ghost": [False, NOW]} and reg._meta == {}


def test_project_without_files_exists(pypi):
    """PIL, elementtree: registered years ago, nothing to download. v0.2 said "exists"."""
    reg, answers, _ = pypi
    answers["pil"] = partial(listing(versions=[], status="archived"))
    assert reg.exists("PIL") is True
    assert reg.info("PIL") is None


@pytest.mark.parametrize(
    "answer",
    [
        http_error(429),
        http_error(500),
        http_error(503),
        http_error(403),
        http_error(410),
        http_error(301),
        http_error(302),
        http_error(308),
        FakeResponse(b"<html>Sign in to the hotel Wi-Fi</html>", 200, content_type="text/html"),
        FakeResponse(listing(ago(3)), 200, content_type="application/json"),
        FakeResponse(listing(ago(3)), 200, content_type="text/plain"),
        FakeResponse(b"", 204),
        FakeResponse(listing(ago(3)), 203),
        urllib.error.URLError("no route to host"),
        TimeoutError("timed out"),
        ConnectionResetError("reset by peer"),
        ssl.SSLCertVerificationError("not pypi.org's certificate"),
        http.client.BadStatusLine("not HTTP at all"),
        http.client.IncompleteRead(b"half"),
        http.client.LineTooLong("header line"),
    ],
    ids=lambda answer: getattr(answer, "code", None) or type(answer).__name__,
)
def test_no_answer_from_pypi_is_unknown_and_ends_the_lookups(pypi, answer):
    """Rate limits, outages, redirects, proxies and dead links: nothing is cached, nothing
    more is sent in this run, and the next run asks again."""
    reg, answers, requests = pypi
    answers["x"] = answer
    answers["y"] = partial(listing(ago(3)))
    assert reg.exists("x") is None
    assert reg.info("x") is None
    assert reg.exists("y") is None and reg.exists("x") is None
    reg.prefetch(["y", "z"])
    assert names(requests) == ["x"]
    reg.save()
    assert not reg.cache_file.exists()  # nothing was cached
    assert PyPIRegistry(reg.cache_file.parent, now=NOW).exists("y") is True  # the next run asks


@pytest.mark.parametrize("status", [200, 206])
def test_a_body_that_breaks_off_is_no_answer(pypi, status):
    """The connection ends before the bytes that ``Content-Length`` promised: PyPI did
    not answer. Calling the package known would keep SLOP002 from it for a week."""
    reg, answers, requests = pypi
    body = gzip.compress(listing(ago(3)))
    half = FakeResponse(
        body[: len(body) // 2],
        status,
        Content_Encoding="gzip",
        Content_Length=str(len(body)),
        Content_Range=f"bytes 0-{len(body) - 1}/{len(body)}",
    )
    answers["young-lib"] = half
    answers["next"] = partial(listing(ago(3)))
    assert reg.exists("young-lib") is None and reg.info("young-lib") is None
    assert reg.exists("next") is None and names(requests) == ["young-lib"]
    reg.save()
    assert not reg.cache_file.exists()
    # The whole body, with the same headers, is an answer like any other.
    whole = FakeResponse(body, status, **{k.replace("-", "_"): v for k, v in half.headers.items()})
    answers["young-lib"] = whole
    later = PyPIRegistry(reg.cache_file.parent, now=NOW)
    assert later.exists("young-lib") is True and later.info("young-lib") is not None
    assert whole.length == 0


def test_other_forms_of_the_simple_api_mean_exists(pypi):
    """If PyPI answers with the HTML form, the package exists and nothing more is known."""
    reg, answers, requests = pypi
    html = FakeResponse(
        b"<a href='x'>x</a>", 200, content_type="application/vnd.pypi.simple.v1+html"
    )
    answers["requests"] = html
    assert reg.exists("requests") is True and reg.info("requests") is None
    assert reg.exists("requests") is True
    assert len(requests) == 1 and html.reads == 0  # cached like any package; the body is not read
    assert reg._cache == {"requests": [True, NOW]}


def test_an_answer_that_cannot_be_read_is_exists_with_nothing_known(pypi, monkeypatch):
    """PyPI said yes, but the listing is broken, cut short, too big or oddly packed."""
    reg, answers, requests = pypi
    doc = listing(ago(3))
    answers["broken"] = FakeResponse(b"not gzip at all", 200, Content_Encoding="gzip")
    answers["cut-gzip"] = FakeResponse(gzip.compress(doc)[:-9], 200, Content_Encoding="gzip")
    answers["cut-text"] = FakeResponse(doc[: len(doc) // 2])
    answers["cut-range"] = FakeResponse(
        gzip.compress(doc)[:40], 206, Content_Encoding="gzip", Content_Range="bytes 0-200/201"
    )
    answers["empty"] = zipped(doc)
    answers["empty"]._body = b""
    answers["brotli"] = FakeResponse(doc, 200, Content_Encoding="br")
    answers["not-json"] = zipped(b"<html>but with PyPI's content type</html>")
    for name in list(answers):
        assert reg.exists(name) is True, name
        assert reg.info(name) is None, name
    assert len(requests) == len(answers) and reg._meta == {}


def test_no_more_text_is_unpacked_than_the_limit(pypi, monkeypatch):
    """A small answer can unpack to a huge text. Past the limit it is not unpacked, and
    what was unpacked is not read as a listing."""
    reg, answers, _ = pypi
    doc = listing(ago(3)) + b" " * 5000  # a listing with facts, were it read whole
    answers["fits"] = zipped(doc)
    answers["bomb"] = zipped(doc)
    assert reg.exists("fits") is True and reg.info("fits") == PackageInfo(
        NOW - 3 * DAY, 1, "active"
    )
    unpacked = []
    real = registry.zlib.decompressobj

    def decompressobj(**kwargs):
        """Hand out the real thing, and record how much text each call to it gives."""
        unzip = real(**kwargs)

        class Recording:
            """A decompressor that notes the size of what it unpacks."""

            def decompress(self, data, max_length=0):
                """Unpack, and note the size."""
                text = unzip.decompress(data, max_length)
                unpacked.append(len(text))
                return text

            def __getattr__(self, name):
                """Everything else is the real decompressor's."""
                return getattr(unzip, name)

        return Recording()

    monkeypatch.setattr(registry.zlib, "decompressobj", decompressobj)
    monkeypatch.setattr(registry, "MAX_LISTING_TEXT", len(doc) - 1)
    assert reg.exists("bomb") is True and reg.info("bomb") is None
    assert unpacked == [len(doc) - 1] and reg._cache["bomb"] == [True, NOW]


def test_identity_and_gzip_answers_read_the_same(pypi):
    """A server may send the listing unpacked, or name the encoding in capitals."""
    reg, answers, _ = pypi
    doc = listing(ago(7), ago(2))
    answers["plain"] = FakeResponse(doc, 200, Content_Encoding="identity")
    answers["capitals"] = FakeResponse(gzip.compress(doc), 200, Content_Encoding=" GZIP ")
    for name in ("plain", "capitals"):
        assert reg.exists(name) is True
        assert reg.info(name) == PackageInfo(NOW - 7 * DAY, 2, "active")


@pytest.mark.parametrize(
    "content_range",
    [None, "", "bytes */500", "bytes 5-104/100", "items 0-99/100", "bytes 0-" + "9" * 5000 + "/1"],
)
def test_ranged_answer_with_an_odd_content_range_counts_as_cut_off(pypi, content_range):
    """Without a clear "these are all the bytes", the listing is not read as a whole one."""
    reg, answers, requests = pypi
    doc = listing(ago(5))
    headers = {} if content_range is None else {"Content_Range": content_range}
    answers["odd"] = [zipped(doc, 206, **headers), zipped(doc)]
    assert reg.exists("odd") is True
    # Young as far as it was read, so the whole listing was asked for.
    assert len(requests) == 2 and reg.info("odd") == PackageInfo(NOW - 5 * DAY, 1, "active")
    answers["odd-and-old"] = zipped(listing(ago(900)), 206, **headers)
    assert reg.exists("odd-and-old") is True and len(requests) == 3


def test_cut_off_listing_without_upload_times_is_read_whole(pypi):
    """No upload time in the part that was read says nothing about the age. A first
    file with a ``requires-python`` of 50 KB would hide a new package that way, so the
    whole listing is asked for, as for a package that looks young."""
    reg, answers, requests = pypi
    padding = "".join(f"!={n}.{n * 7919 % 1009}," for n in range(9000))
    file = {"filename": "pkg-1.whl", "requires-python": padding, "upload-time": ago(5)}
    doc = json.dumps({"files": [file], "versions": ["1"]}).encode()
    answers["padded"] = [partial(doc), zipped(doc)]
    assert b"upload-time" not in gzip.decompress(gzip.compress(doc))[:LISTING_LIMIT]
    assert reg.exists("padded") is True
    assert reg.info("padded") == PackageInfo(NOW - 5 * DAY, 1, None)
    assert [r.get_header("Range") for r in requests] == [f"bytes=0-{LISTING_LIMIT - 1}", None]
    # A whole listing that has no upload times either: the package exists, and that is all.
    bare = b'{"files": [' + b'{"filename": "a.whl"},' * 5000 + b'{"filename": "b.whl"}]}'
    answers["bare"] = [partial(bare), zipped(bare)]
    assert len(gzip.compress(bare)) < LISTING_LIMIT  # so make it a cut-off answer by hand
    answers["bare"][0].headers.replace_header("Content-Range", "bytes 0-99/100000")
    assert reg.exists("bare") is True and reg.info("bare") is None
    assert names(requests[2:]) == ["bare", "bare"]


def test_slow_answer_counts_as_unreachable(pypi, monkeypatch):
    """A link too slow to deliver the listing in time: unknown, and no more requests."""
    reg, answers, requests = pypi
    slow = FakeResponse(listing(*[ago(3)] * 80))
    answers["slow"] = slow
    answers["next"] = FakeResponse(listing(ago(3)))
    monkeypatch.setattr(registry, "READ_DEADLINE", -1.0)  # time is up before the first byte
    assert reg.exists("slow") is None
    assert reg.exists("next") is None
    assert len(requests) == 1 and slow.reads == 0 and reg._cache == {}


def test_deadline_is_checked_between_the_parts_of_a_body(pypi, monkeypatch):
    """The time limit holds for the whole body, however many parts it comes in."""
    reg, answers, _ = pypi
    clock = iter(range(0, 1000, 4))  # each look at the clock is four seconds later
    monkeypatch.setattr(registry.time, "monotonic", lambda: next(clock))
    slow = FakeResponse(listing(*[ago(3)] * 80))
    answers["slow"] = slow
    assert registry.READ_DEADLINE == 10.0
    assert reg.exists("slow") is None
    assert slow.reads == 2  # read at 4 s and at 8 s, given up at 12 s


# --- the cache --------------------------------------------------------------------------


def test_cache_file_keeps_v02_entries_and_adds_facts(pypi, tmp_path):
    """Entries stay [found, time] so v0.2 can read the file; facts live under "_meta"."""
    reg, answers, requests = pypi
    answers["newpkg"] = FakeResponse(listing(ago(6)))
    answers["oldpkg"] = FakeResponse(listing(ago(900)))
    answers["ghost"] = http_error(404, "ghost")
    assert reg.exists("newpkg") and reg.exists("oldpkg") and reg.exists("ghost") is False
    reg.save()
    data = json.loads((tmp_path / "pypi.json").read_text(encoding="utf-8"))
    assert data == {
        "newpkg": [True, NOW],
        "oldpkg": [True, NOW],
        "ghost": [False, NOW],
        "_meta": {"newpkg": [NOW - 6 * DAY, 1, "active", NOW]},
    }
    again = PyPIRegistry(tmp_path, now=NOW + 3600)
    assert again.exists("newpkg") is True
    assert again.info("newpkg") == reg.info("newpkg") == PackageInfo(NOW - 6 * DAY, 1, "active")
    assert again.exists("ghost") is False and again.exists("oldpkg") is True
    assert len(requests) == 3  # the second registry asked nothing
    again.save()  # and has nothing to write
    assert json.loads((tmp_path / "pypi.json").read_text(encoding="utf-8")) == data


def test_v02_and_pinned_caches_answer_without_requests(tmp_path, monkeypatch):
    """A v0.2 cache, or one pinned in CI, has no facts: nothing is asked or reported."""
    pinned = {"requests": [True, 9e12], "flask-jwt-simple-auth": [False, 1791739900]}
    (tmp_path / "pypi.json").write_text(json.dumps(pinned), encoding="utf-8")
    monkeypatch.setattr(PyPIRegistry, "_query", lambda self, key: pytest.fail(f"lookup of {key}"))
    reg = PyPIRegistry(tmp_path, now=1791739900 + 3600)
    reg.prefetch(["requests", "Flask_JWT.simple-auth"])
    assert reg.exists("requests") is True
    assert reg.info("requests") is None
    assert reg.exists("flask-jwt-simple-auth") is False
    reg.save()
    assert json.loads((tmp_path / "pypi.json").read_text(encoding="utf-8")) == pinned


def test_answers_that_can_lead_to_a_finding_are_asked_again_after_a_day(pypi, tmp_path):
    """Missing and young packages: a day. Established ones: a week."""
    reg, answers, requests = pypi
    answers["young"] = FakeResponse(listing(ago(40)))
    answers["old"] = FakeResponse(listing(ago(4000)))
    answers["ghost"] = http_error(404, "ghost")
    reg.prefetch(["young", "old", "ghost"])
    reg.save()
    assert len(requests) == 3 and (FACTS_TTL, MISSING_TTL, EXISTS_TTL) == (DAY, DAY, 7 * DAY)

    later = PyPIRegistry(tmp_path, now=NOW + 0.99 * DAY)
    later.prefetch(["young", "old", "ghost"])
    assert len(requests) == 3

    answers["young"] = FakeResponse(listing(ago(40), ago(1)))  # a second release came out
    next_day = PyPIRegistry(tmp_path, workers=1, now=NOW + DAY)
    next_day.prefetch(["young", "old", "ghost"])
    assert names(requests[3:]) == ["young", "ghost"]
    assert next_day.info("young").releases == 2 and next_day.exists("old") is True
    next_day.save()

    answers["old"] = FakeResponse(listing(ago(4000)))
    assert PyPIRegistry(tmp_path, now=NOW + 7 * DAY - 1).exists("old") is True
    assert len(requests) == 5
    assert PyPIRegistry(tmp_path, now=NOW + 7 * DAY).exists("old") is True
    assert names(requests[5:]) == ["old"]


def test_facts_are_asked_again_even_if_v02_refreshed_the_entry(tmp_path, monkeypatch):
    """v0.2 rewrites the entry and keeps "_meta": the facts still expire after a day."""
    first = NOW - 100 * DAY
    shared = {"pkg": [True, NOW - 60], "_meta": {"pkg": [first, 1, "active", NOW - 8 * DAY]}}
    (tmp_path / "pypi.json").write_text(json.dumps(shared), encoding="utf-8")
    asked = []

    def query(self, key):
        """PyPI now knows a second release."""
        asked.append(key)
        return Lookup(True, PackageInfo(first, 2, "active"))

    monkeypatch.setattr(PyPIRegistry, "_query", query)
    reg = PyPIRegistry(tmp_path, now=NOW)
    assert reg.info("pkg") is None  # facts that old are not given out
    assert reg.exists("pkg") is True
    assert asked == ["pkg"] and reg.info("pkg") == PackageInfo(first, 2, "active")


def test_stale_facts_never_reach_the_rule_when_pypi_is_down(tmp_path, monkeypatch):
    """Old facts and no way to refresh them: the answer is unknown, not the old facts."""
    stale = {"pkg": [True, NOW - 60], "_meta": {"pkg": [NOW - 9 * DAY, 1, "active", NOW - 2 * DAY]}}
    (tmp_path / "pypi.json").write_text(json.dumps(stale), encoding="utf-8")
    monkeypatch.setattr(PyPIRegistry, "_query", lambda self, key: None)
    reg = PyPIRegistry(tmp_path, now=NOW)
    assert reg.exists("pkg") is None and reg.info("pkg") is None
    # A day earlier the same file answers by itself.
    fresh = PyPIRegistry(tmp_path, now=NOW - 1.5 * DAY)
    assert fresh.exists("pkg") is True and fresh.info("pkg").releases == 1


def test_facts_are_dropped_when_an_answer_changes(pypi, tmp_path):
    """A refreshed entry replaces the old facts; a package that vanished loses them."""
    reg, answers, _ = pypi
    answers["pkg"] = FakeResponse(listing(ago(5)))
    assert reg.exists("pkg") and reg.info("pkg") is not None
    reg.save()
    answers["pkg"] = partial(long_listing(4000))
    later = PyPIRegistry(tmp_path, now=NOW + 2 * DAY)
    assert later.exists("pkg") is True and later.info("pkg") is None
    later.save()
    assert "_meta" not in json.loads((tmp_path / "pypi.json").read_text(encoding="utf-8"))
    answers["pkg"] = http_error(404, "pkg")
    gone = PyPIRegistry(tmp_path, now=NOW + 20 * DAY)
    assert gone.exists("pkg") is False and gone.info("pkg") is None


def test_batch_stores_facts_like_single_lookups(pypi):
    """Eight requests at a time: the same answers and facts, stored on the calling thread."""
    reg, answers, requests = pypi
    for n in range(30):
        answers[f"new-{n}"] = FakeResponse(listing(ago(n + 1)))
        answers[f"old-{n}"] = FakeResponse(listing(ago(400 + n)))
        answers[f"ghost-{n}"] = http_error(404)
    reg.prefetch(answers)
    assert len(requests) == 90 and sorted(names(requests)) == sorted(answers)
    assert all(
        reg.info(f"new-{n}") == PackageInfo(NOW - (n + 1) * DAY, 1, "active") for n in range(30)
    )
    assert all(reg.exists(f"old-{n}") and reg.info(f"old-{n}") is None for n in range(30))
    assert all(reg.exists(f"ghost-{n}") is False for n in range(30))
    assert list(reg._cache) == list(answers) and len(reg._meta) == 30  # in the order given
    assert len(requests) == 90


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[1, 2]",
        pytest.param("[" * 100_000, id="nested too deeply"),
        '{"pkg": "yes", "_meta": "broken"}',
        '{"pkg": [true, 9e12, "extra"], "_meta": {"pkg": [1, 2]}}',
        '{"pkg": [true, "tomorrow"], "_meta": {"pkg": [5, 1, null, 9e12]}}',
        '{"pkg": ["yes", 9e12], "_meta": {"pkg": [5, 1, null, 9e12]}}',
        '{"pkg": [true, true], "_meta": {"pkg": [5, 1, null, 9e12]}}',
        '{"pkg": [true, NaN], "_meta": {"pkg": [5, 1, null, NaN]}}',
        '{"pkg": [1, 9e12], "_meta": {"pkg": [5, 1, null, 9e12]}}',
        '{"pkg": {"found": true}, "_meta": {"pkg": [5, 1, null, 9e12]}}',
        '{"pkg": [true, 9e12], "_meta": {"pkg": ["soon", 1, "active", 9e12]}}',
        '{"pkg": [true, 9e12], "_meta": {"pkg": [true, 1, "active", 9e12]}}',
        '{"pkg": [true, 9e12], "_meta": {"pkg": [5.5, 1, "active", 9e12]}}',
        '{"pkg": [true, 9e12], "_meta": {"pkg": [5, "many", null, 9e12]}}',
        '{"pkg": [true, 9e12], "_meta": {"pkg": [5, true, null, 9e12]}}',
        '{"pkg": [true, 9e12], "_meta": {"pkg": [5, -1, null, 9e12]}}',
        '{"pkg": [true, 9e12], "_meta": {"pkg": [5, 1, 7, 9e12]}}',
        '{"pkg": [true, 9e12], "_meta": {"pkg": [5, 1, "active", "now"]}}',
        '{"pkg": [true, 9e12], "_meta": {"pkg": [5, 1, "active", true]}}',
        '{"pkg": [true, 9e12], "_meta": {"pkg": [5, 1, "active"]}}',
        '{"pkg": [true, 9e12], "_meta": {"pkg": {"first": 5}}}',
        '{"pkg": [true, 9e12], "_meta": ["pkg", 5, 1]}',
        '{"pkg": [false, 9e12], "_meta": {"pkg": [5, 1, "active", 9e12]}}',
        pytest.param('{"pkg": [true, %s]}' % ("9" * 400), id="time of 400 digits"),
        pytest.param(
            '{"pkg": [true, 9e12], "_meta": {"pkg": [5, 1, "active", %s]}}' % ("9" * 400),
            id="facts with a time of 400 digits",
        ),
        pytest.param(
            '{"pkg": [true, 9e12], "_meta": {"pkg": [%s, 1, "active", 9e12]}}' % ("9" * 400),
            id="upload time of 400 digits",
        ),
        '{"pkg": [true, -Infinity], "_meta": {"pkg": [5, 1, "active", -Infinity]}}',
    ],
)
def test_damaged_cache_never_crashes_or_invents_facts(tmp_path, monkeypatch, content):
    """Whatever is in the file: no crash, and no facts that PyPI didn't give."""
    (tmp_path / "pypi.json").write_text(content, encoding="utf-8")
    monkeypatch.setattr(PyPIRegistry, "_query", lambda self, key: None)
    reg = PyPIRegistry(tmp_path, now=NOW + 0.5)  # a float, as the clock gives it
    reg.prefetch(["pkg", "other"])
    assert reg.exists("pkg") in (True, False, None)
    assert reg.info("pkg") is None
    reg.save()  # nothing changed, so the file is left as it was
    assert (tmp_path / "pypi.json").read_text(encoding="utf-8") == content


def test_a_new_answer_replaces_a_damaged_entry(tmp_path, monkeypatch):
    """A damaged entry is looked up again, and what PyPI says takes its place."""
    damaged = '{"pkg": [true, "tomorrow"], "_meta": {"pkg": "?", "kept": [5, 1, null, 9e12]}}'
    (tmp_path / "pypi.json").write_text(damaged, encoding="utf-8")
    info = PackageInfo(NOW - 3 * DAY, 1, "active")
    monkeypatch.setattr(PyPIRegistry, "_query", lambda self, key: Lookup(True, info))
    reg = PyPIRegistry(tmp_path, now=NOW)
    assert reg.exists("pkg") is True and reg.info("pkg") == info
    reg.save()
    assert json.loads((tmp_path / "pypi.json").read_text(encoding="utf-8")) == {
        "pkg": [True, NOW],
        "_meta": {"pkg": [NOW - 3 * DAY, 1, "active", NOW], "kept": [5, 1, None, 9e12]},
    }


def test_the_clock_is_read_once_per_registry(tmp_path, monkeypatch):
    """Without ``now``, the time of the run is the clock's when the registry is made."""
    monkeypatch.setattr(registry.time, "time", lambda: 5000.0)
    reg = PyPIRegistry(tmp_path)
    monkeypatch.setattr(registry.time, "time", lambda: pytest.fail("the clock was read again"))
    monkeypatch.setattr(PyPIRegistry, "_query", lambda self, key: Lookup(True))
    assert reg.exists("pkg") is True and reg._cache == {"pkg": [True, 5000.0]}


def test_only_some_registries_have_facts(tmp_path):
    """``info`` is optional: the registries from before SLOP002 work without it."""
    assert isinstance(PyPIRegistry(tmp_path, now=NOW), FactsRegistry)
    assert isinstance(FakeFactsRegistry(set(), {}), FactsRegistry)
    for plain in (FakeRegistry(set()), OfflineRegistry(), DownRegistry()):
        assert not isinstance(plain, FactsRegistry)
