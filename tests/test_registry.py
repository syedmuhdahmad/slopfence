"""PyPI lookups as one concurrent batch, with the same findings (#54)."""

import io
import json
import os
import threading
import time
import urllib.error

import pytest

from slopfence.engine import run
from slopfence.registry import (
    EXISTS_TTL,
    LOOKUP_WORKERS,
    MISSING_TTL,
    BatchRegistry,
    Lookup,
    OfflineRegistry,
    PyPIRegistry,
)
from tests.conftest import FakeBatchRegistry, FakeRegistry

EXISTING = {"requests", "pytest"}


@pytest.fixture
def project(write, tmp_path):
    """Dependencies and imports that may be looked up, next to ones that must not be."""
    write(
        "requirements.txt",
        "requests\nghost-dep-zz\ncorp-auth\nurl-dep @ https://x.example/u.whl\n",
    )
    write("more/requirements-dev.txt", "pytest\nother-ghost-zz  # slopfence: ignore\n")
    write("src/app.py", "import os\nimport requests\nimport ghost_mod_zz\nimport corp_auth\n")
    write("src/other.py", "import ghost_mod_zz\nimport second_ghost_zz, os\n")
    return tmp_path


def scan(root, registry, **kwargs):
    """Run SLOP001 with import checks on, unless told otherwise."""
    kwargs.setdefault("check_imports", True)
    return run([root], root, registry, select=["SLOP001"], known_packages=["corp-*"], **kwargs)


def test_only_some_registries_take_a_batch(tmp_path):
    """``prefetch`` is optional: PyPI has it, and the simple registries work without it."""
    assert isinstance(PyPIRegistry(tmp_path / "cache"), BatchRegistry)
    assert isinstance(FakeBatchRegistry(set()), BatchRegistry)
    assert not isinstance(FakeRegistry(set()), BatchRegistry)
    assert not isinstance(OfflineRegistry(), BatchRegistry)


def test_batch_gives_the_same_findings(project):
    """A registry with prefetch gets every name up front; the findings don't change."""
    one_by_one, batched = FakeRegistry(EXISTING), FakeBatchRegistry(EXISTING)
    expected = scan(project, one_by_one).findings
    assert scan(project, batched).findings == expected
    assert [(f.path, f.line) for f in expected] == [
        ("requirements.txt", 2),
        ("src/app.py", 3),
        ("src/other.py", 1),
        ("src/other.py", 2),
    ]
    # One batch, with exactly the names that are asked about afterwards and no others.
    [batch] = batched.batches
    assert set(batch) == set(batched.queries) == set(one_by_one.queries)
    assert len(batch) == len(set(batch))


def test_batch_never_holds_names_that_must_not_be_sent(project, write):
    """Known packages, ignored lines, URLs and (without check-imports) imports stay out."""
    registry = FakeBatchRegistry(set())
    scan(project, registry, check_imports=False)
    assert registry.batches == [["pytest", "requests", "ghost-dep-zz"]]  # in file order

    # A private index in a dependency file: neither its packages nor any import is sent.
    write("requirements.txt", "--extra-index-url https://pkgs.corp.example/simple\nrequests\n")
    registry = FakeBatchRegistry(set())
    scan(project, registry)
    assert registry.batches == [["pytest"]] and registry.queries == ["pytest"]

    # A private index in the environment: nothing at all.
    registry = FakeBatchRegistry(set())
    scan(project, registry, private_index="the PIP_INDEX_URL environment variable")
    assert registry.batches == [] and registry.queries == []


def test_imports_outside_the_diff_are_not_sent(project):
    """With --diff, only changed files have their imports looked up."""
    registry = FakeBatchRegistry(set())
    scan(project, registry, changed={"src/app.py": None})
    [batch] = registry.batches
    assert "second_ghost_zz" not in batch and "ghost_mod_zz" in batch
    assert set(batch) == set(registry.queries)


def test_offline_run_collects_and_sends_nothing(project, monkeypatch):
    """--offline: no import is prepared for a lookup, and no name is sent."""
    from slopfence.detectors import packages

    def no_lookup(*args):
        """Fail the test: an offline run has no reason to look at what is installed."""
        raise AssertionError("an import was prepared for a lookup")

    monkeypatch.setattr(packages, "_is_installed", no_lookup)
    assert scan(project, OfflineRegistry()).findings == []


@pytest.fixture
def pypi(tmp_path, monkeypatch):
    """A PyPIRegistry whose network is a thread-safe fake: names with "ghost" are missing."""
    asked: list[str] = []
    lock = threading.Lock()

    def query(self, key):
        """Answer like PyPI would, and record the request."""
        with lock:
            asked.append(key)
        return Lookup("ghost" not in key)

    monkeypatch.setattr(PyPIRegistry, "_query", query)
    return PyPIRegistry(tmp_path / "cache"), asked


def test_eight_requests_are_in_flight_at_once(tmp_path, monkeypatch):
    """By default a batch has 8 requests in flight: not fewer, and never more."""
    in_flight = []
    most = []
    together = threading.Barrier(LOOKUP_WORKERS)

    def query(self, key):
        """Wait until 8 requests are in flight, then answer."""
        in_flight.append(key)
        most.append(len(in_flight))
        together.wait(timeout=10)  # times out, and fails the test, with fewer than 8 threads
        in_flight.remove(key)
        return Lookup(True)

    monkeypatch.setattr(PyPIRegistry, "_query", query)
    registry = PyPIRegistry(tmp_path / "cache")
    registry.prefetch(f"pkg-{n}" for n in range(5 * LOOKUP_WORKERS))
    assert LOOKUP_WORKERS == 8 and max(most) == 8
    assert len(registry._cache) == 40


def test_prefetch_asks_once_per_name_and_fills_the_cache(pypi):
    """Names are normalized and looked up once; later questions cost no request."""
    registry, asked = pypi
    names = [f"pkg-{n}" for n in range(300)] + ["Ghost_Pkg", "ghost.pkg", "pkg-0"]
    registry.prefetch(names)
    assert sorted(asked) == sorted({f"pkg-{n}" for n in range(300)} | {"ghost-pkg"})
    assert registry.exists("pkg-7") is True and registry.exists("Ghost-Pkg") is False
    registry.prefetch(names)  # everything is fresh in the cache now
    assert len(asked) == 301


def test_single_lookups_still_work(pypi):
    """Without a batch, ``exists`` sends one request per new name, as before."""
    registry, asked = pypi
    assert registry.exists("Some_Pkg") is True
    assert registry.exists("ghost-pkg") is False
    assert registry.exists("some-pkg") is True
    assert asked == ["some-pkg", "ghost-pkg"]
    registry.prefetch(["one-name"])
    assert asked == ["some-pkg", "ghost-pkg", "one-name"]


def test_one_name_or_one_worker_starts_no_thread(tmp_path, monkeypatch):
    """A single request, or a registry told to send one at a time, stays on this thread."""
    threads = []

    def query(self, key):
        """Record which thread sends the request."""
        threads.append(threading.current_thread())
        return Lookup(True)

    monkeypatch.setattr(PyPIRegistry, "_query", query)
    PyPIRegistry(tmp_path / "a").prefetch(["only-one"])
    PyPIRegistry(tmp_path / "b", workers=1).prefetch(["one", "two", "three"])
    assert threads == [threading.main_thread()] * 4
    PyPIRegistry(tmp_path / "c").prefetch(["one", "two"])
    assert all(t.name.startswith("slopfence-pypi") for t in threads[4:]) and len(threads) == 6


def test_one_thread_or_many_give_the_same_cache(tmp_path, pypi):
    """The answers don't depend on how many requests are in flight at once."""
    _, asked = pypi
    names = [f"pkg-{n}" for n in range(40)] + [f"ghost-{n}" for n in range(10)]
    caches = []
    for workers in (1, 8):
        registry = PyPIRegistry(tmp_path / f"cache-{workers}", workers=workers)
        registry.prefetch(names)
        caches.append({key: found for key, (found, _) in registry._cache.items()})
    assert caches[0] == caches[1] and len(caches[0]) == 50
    assert list(caches[0]) == list(caches[1])  # stored in the order the names were given
    assert len(asked) == 100


def test_cache_file_is_written_once(pypi, monkeypatch):
    """Looking up writes nothing; save() writes the whole cache in one step."""
    registry, _ = pypi
    replaced = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: replaced.append(b) or real(a, b))
    registry.prefetch(f"pkg-{n}" for n in range(50))
    assert not registry.cache_file.parent.exists()
    registry.save()
    assert replaced == [registry.cache_file]
    assert [p.name for p in registry.cache_file.parent.iterdir()] == ["pypi.json"]
    registry.save()  # nothing new to write
    assert len(replaced) == 1


def test_cache_file_keeps_its_shape(pypi, tmp_path):
    """``{name: [found, timestamp]}``: what v0.2 wrote, and what CI pins answers in."""
    registry, asked = pypi
    registry.prefetch(["Real_Pkg", "ghost-pkg"])
    registry.save()
    saved = json.loads(registry.cache_file.read_text(encoding="utf-8"))
    assert {name: found for name, (found, _) in saved.items()} == {
        "real-pkg": True,
        "ghost-pkg": False,
    }
    assert all(isinstance(stamp, float) for _, stamp in saved.values())
    # A new run reads the file and has nothing to ask.
    again = PyPIRegistry(tmp_path / "cache")
    again.prefetch(["real-pkg", "ghost_pkg"])
    assert again.exists("real-pkg") is True and again.exists("ghost-pkg") is False
    assert len(asked) == 2


def test_old_answers_are_asked_again(tmp_path, pypi):
    """An answer is trusted for a week if the package exists and for a day if it doesn't."""
    _, asked = pypi
    now = time.time()
    pinned = {
        "fresh-pkg": [True, now - EXISTS_TTL + 60],
        "old-pkg": [True, now - EXISTS_TTL - 60],
        "fresh-ghost": [False, now - MISSING_TTL + 60],
        "old-ghost": [False, now - MISSING_TTL - 60],
    }
    cache_dir = tmp_path / "pinned"
    cache_dir.mkdir()
    (cache_dir / "pypi.json").write_text(json.dumps(pinned), encoding="utf-8")
    registry = PyPIRegistry(cache_dir)
    registry.prefetch(pinned)
    assert sorted(asked) == ["old-ghost", "old-pkg"]


@pytest.mark.parametrize("workers", [1, 8])
def test_unreachable_registry_stops_the_batch(tmp_path, monkeypatch, workers):
    """After the first failed request no new request starts, and nothing is "missing"."""
    asked: list[str] = []
    together = threading.Barrier(workers)

    def query(self, key):
        """Fail like a network error, once ``workers`` requests are in flight."""
        asked.append(key)
        together.wait(timeout=10)
        self._unreachable = True
        return None

    monkeypatch.setattr(PyPIRegistry, "_query", query)
    registry = PyPIRegistry(tmp_path / "cache", workers=workers)
    registry.prefetch(f"pkg-{n}" for n in range(100))
    assert len(asked) == workers  # only the requests that were already in flight
    assert registry.exists("pkg-99") is None and len(asked) == workers
    registry.save()
    assert not registry.cache_file.exists()  # nothing learned, nothing written


def test_answers_before_a_failure_still_count(tmp_path, monkeypatch):
    """What PyPI answered before it became unreachable is kept; the rest is unknown."""

    def query(self, key):
        """Answer the first name, then fail."""
        if key == "pkg-1":
            return Lookup(True)
        self._unreachable = True
        return None

    monkeypatch.setattr(PyPIRegistry, "_query", query)
    registry = PyPIRegistry(tmp_path / "cache", workers=1)
    registry.prefetch(["pkg-1", "pkg-0", "pkg-2"])
    assert registry.exists("pkg-1") is True
    assert registry.exists("pkg-0") is None and registry.exists("pkg-2") is None


def test_unknown_answer_for_one_name_is_not_cached(tmp_path, monkeypatch):
    """A name without an answer is not stored, and the answers for the others are. The
    name stays unknown for this run without a second request, and the next run asks again."""
    asked: list[str] = []

    def query(self, key):
        """Answer every name but one, without calling PyPI unreachable."""
        asked.append(key)
        return None if key == "busy-pkg" else Lookup(True)

    monkeypatch.setattr(PyPIRegistry, "_query", query)
    registry = PyPIRegistry(tmp_path / "cache", workers=1)
    registry.prefetch(["pkg-1", "busy-pkg", "pkg-2"])
    assert registry.exists("pkg-1") is True and registry.exists("pkg-2") is True
    assert registry.exists("busy-pkg") is None and registry.exists("Busy_Pkg") is None
    registry.prefetch(["busy-pkg"])
    assert asked == ["pkg-1", "busy-pkg", "pkg-2"]
    registry.save()
    # Not remembered between runs: the next run sends the request again.
    again = PyPIRegistry(tmp_path / "cache", workers=1)
    assert again.exists("pkg-1") is True and again.exists("busy-pkg") is None
    assert asked == ["pkg-1", "busy-pkg", "pkg-2", "busy-pkg"]


def test_single_lookup_without_an_answer_is_not_repeated(tmp_path, monkeypatch):
    """Without a batch too: a name PyPI gave no answer for is requested once per run."""
    asked: list[str] = []
    monkeypatch.setattr(PyPIRegistry, "_query", lambda self, key: asked.append(key))
    registry = PyPIRegistry(tmp_path / "cache")
    assert registry.exists("busy-pkg") is None and registry.exists("busy-pkg") is None
    assert asked == ["busy-pkg"]


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("workers", [1, 8])
def test_rate_limited_run_stops_asking(write, tmp_path, monkeypatch, status, workers):
    """End to end: PyPI answers with an error status. No new request starts after the
    first such answer: only the ones already in flight were sent, each for another
    name. Nothing is reported, and nothing is cached."""
    requested: list[str] = []

    def no_answer(self, request):
        """Answer like PyPI when it limits requests or is down. No socket is opened."""
        requested.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, status, "No answer", {}, io.BytesIO())

    monkeypatch.setattr(PyPIRegistry, "_open", no_answer)
    names = [f"pkg-number-{n}" for n in range(200)]
    write("requirements.txt", "".join(f"{name}\n" for name in names))
    registry = PyPIRegistry(tmp_path / "cache", workers=workers)
    result = run([tmp_path], tmp_path, registry, select=["SLOP001"])
    assert result.findings == []
    assert 1 <= len(requested) <= workers and len(set(requested)) == len(requested)
    assert set(requested) <= {f"https://pypi.org/simple/{name}/" for name in names}
    registry.save()
    assert not registry.cache_file.exists()


def test_ctrl_c_does_not_wait_for_requests_in_flight(tmp_path, monkeypatch):
    """Ctrl-C during a batch: the requests that are out are not waited for (each could
    take the whole timeout), the ones not sent yet are dropped, and the interrupt goes on."""
    import concurrent.futures

    shutdowns: list[dict] = []

    class InterruptedPool:
        """Stands in for ThreadPoolExecutor while the user presses Ctrl-C."""

        def __init__(self, **kwargs):
            """Accept the real pool's arguments."""

        def map(self, *args):
            """Be interrupted while the answers are collected."""
            raise KeyboardInterrupt

        def shutdown(self, **kwargs):
            """Record how the pool was shut down."""
            shutdowns.append(kwargs)

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", InterruptedPool)
    registry = PyPIRegistry(tmp_path / "cache")
    with pytest.raises(KeyboardInterrupt):
        registry.prefetch(["pkg-1", "pkg-2"])
    assert shutdowns == [{"wait": False, "cancel_futures": True}]


def test_unreachable_registry_reports_nothing(project, monkeypatch, tmp_path):
    """End to end: PyPI can't be reached during the batch, so there are no findings."""

    def query(self, key):
        """Fail like a network error."""
        self._unreachable = True
        return None

    monkeypatch.setattr(PyPIRegistry, "_query", query)
    assert scan(project, PyPIRegistry(tmp_path / "cache")).findings == []


def test_real_registry_gives_the_same_findings_as_one_by_one(project, pypi):
    """End to end through the thread pool: same findings, same order, as one at a time."""
    registry, asked = pypi
    expected = scan(project, FakeRegistry(EXISTING)).findings
    assert len(expected) == 4
    assert scan(project, registry).findings == expected
    assert len(asked) == len(set(asked))  # no name is requested twice


def test_save_leaves_no_temporary_file_when_it_fails(pypi, monkeypatch):
    """A failed write is ignored, as before, and doesn't leave a half-written file."""
    registry, _ = pypi
    registry.prefetch(["pkg-1"])

    def fail(a, b):
        """Stand in for a full disk or a locked file."""
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    registry.save()  # must not raise
    assert list(registry.cache_file.parent.iterdir()) == []
