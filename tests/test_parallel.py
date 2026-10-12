"""--jobs: files are checked in worker processes, with the same result (#12)."""

import importlib
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
import types
import warnings
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest

from slopfence import analysis
from slopfence import cli as climod
from slopfence.analysis import Options, analyze_files, default_jobs, worker_count
from slopfence.cli import main
from slopfence.config import ConfigError, load_config
from slopfence.detectors import FILE_DETECTORS
from slopfence.engine import run
from tests.conftest import FakeRegistry

HELPER = '''
def format_date(value, fmt="%Y-%m-%d"):
    """Format a date for display."""
    if value is None:
        return ""
    text = value.strftime(fmt)
    return text.strip()
'''
SWALLOW = "def f{n}(x):\n    try:\n        x()\n    except Exception:\n        pass\n"
MOCK_TEST = (
    "from unittest.mock import Mock\n\n\ndef test_x():\n    m = Mock()\n    m.a = 1\n"
    "    assert m.a == 1\n"
)
# The ways a pool may start its workers on this Python: every way the platform has,
# but only "fork" on Python 3.10 (see ``analysis._new_processes_are_safe``).
POOL_METHODS = [
    method
    for method in multiprocessing.get_all_start_methods()
    if sys.version_info >= (3, 11) or method == "fork"
]


@pytest.fixture
def project(write, tmp_path):
    """A project with every kind of result: findings of per-file rules, duplicates, a
    missing import, ignore comments, a test file and a file that can't be parsed."""
    write("src/a.py", "# In a real implementation, retry\n" + HELPER)
    write("src/b.py", "import ghost_pkg_zz\n" + HELPER.replace("format_date", "fmt_date"))
    write("src/c.py", HELPER.replace("format_date", "fmt2") + "# I hope this helps!\n")
    write("src/d.py", "import ghost_pkg_zz  # slopfence: ignore\nx = 1\n")
    write("src/ignored.py", "# slopfence: ignore-file\n# I hope this helps!\n" + HELPER)
    write("src/broken.py", "def broken(:\n")
    write("tests/test_a.py", MOCK_TEST)
    write("requirements.txt", "requests\nghost-dep-zz\n")
    for n in range(6):
        write(f"src/more/m{n}.py", SWALLOW.format(n=n))
    return tmp_path


def scan(root, jobs):
    """Everything a run reports, for comparing runs."""
    result = run([root], root, FakeRegistry({"requests"}), check_imports=True, jobs=jobs)
    return result.findings, result.parse_errors, result.files_checked, result.notes


def tasks_and_options(root):
    """Tasks and options that check every Python file under ``root``."""
    tasks = [(str(p), True) for p in sorted(root.rglob("*.py"))]
    return tasks, Options(str(root), frozenset(FILE_DETECTORS), functions=True, imports=True)


def count_pools(monkeypatch):
    """Make two files enough for a pool. Returns the worker count of each pool started.

    A pool that gives up fails the test. Without that, a test would pass on a machine
    where no worker can start, because the files are then checked in this process.
    """
    pools = []
    real = analysis._analyze_in_pool

    def counted(tasks, options, workers, mp_context):
        """Run the real pool, note its size, and insist that it did the work."""
        pools.append(workers)
        summaries = real(tasks, options, workers, mp_context)
        assert summaries is not None, "the worker processes did not do the work"
        return summaries

    monkeypatch.setattr(analysis, "MIN_FILES_PER_WORKER", 2)
    monkeypatch.setattr(analysis, "_analyze_in_pool", counted)
    return pools


def test_same_result_with_one_job_and_many(project, monkeypatch):
    """--jobs N gives the same findings in the same order as --jobs 1."""
    serial = scan(project, 1)
    assert {f.rule for f in serial[0]} == {
        "SLOP001",
        "SLOP010",
        "SLOP020",
        "SLOP030",
        "SLOP040",
        "SLOP051",
    }
    assert serial[1] == ["src/broken.py"]
    pools = count_pools(monkeypatch)
    assert scan(project, 2) == serial
    assert scan(project, 4) == serial
    assert pools == [2, 4]  # worker processes really did the work


@pytest.mark.parametrize("method", POOL_METHODS)
def test_every_start_method(project, method):
    """spawn (Windows, macOS), forkserver and fork all give the single-process result.
    The pool is called directly: it answers None when the workers could not do the work."""
    tasks, options = tasks_and_options(project)
    serial = analyze_files(tasks, options, 1)
    context = multiprocessing.get_context(method)
    assert analysis._analyze_in_pool(tasks, options, 3, context) == serial


@pytest.mark.parametrize("method", POOL_METHODS)
def test_warning_filters_of_this_process_do_not_change_the_result(write, tmp_path, capfd, method):
    """A program that turned warnings into errors (as ``pytest -W error`` does) gets the
    same result with workers as without. A "spawn" worker doesn't have those filters, so
    a file Python only warns about must parse the same way with them and without."""
    write("escapes.py", 'PATTERN = "\\d+"\n# I hope this helps!\n')
    write("plain.py", "# I hope this helps!\n")
    tasks, options = tasks_and_options(tmp_path)
    with warnings.catch_warnings():
        # Only this warning: Python has others of its own when a process is forked.
        warnings.filterwarnings("error", message=".*invalid escape sequence")
        serial = analyze_files(tasks, options, 1)
        context = multiprocessing.get_context(method)
        assert analysis._analyze_in_pool(tasks, options, 2, context) == serial
    assert [(s.rel, s.parsed, len(s.findings)) for s in serial] == [
        ("escapes.py", True, 1),
        ("plain.py", True, 1),
    ]
    assert capfd.readouterr().err == ""


def test_odd_file_names_cross_the_process_boundary(write, tmp_path, monkeypatch):
    """Spaces and non-ASCII letters in a path reach the workers and come back intact."""
    names = ["with space/a b.py", "ünïcode/naïve.py", "日本/語.py", "plain.py"]
    for name in names:
        write(name, "# I hope this helps!\n")
    tasks, options = tasks_and_options(tmp_path)
    serial = analyze_files(tasks, options, 1)
    assert len(serial) == len(names)
    assert {"with space/a b.py", "plain.py"} < {s.rel for s in serial}
    assert all(s.parsed and [f.rule for f in s.findings] == ["SLOP051"] for s in serial)
    pools = count_pools(monkeypatch)
    assert analyze_files(tasks, options, 2) == serial
    assert pools == [2]


def test_no_pool_for_few_files(project, monkeypatch):
    """Under 200 files nothing is started, whatever --jobs says."""

    def no_pool(*args):
        """Fail the test: this project is far too small for worker processes."""
        raise AssertionError("worker processes were started")

    monkeypatch.setattr(analysis, "_analyze_in_pool", no_pool)
    assert scan(project, 8) == scan(project, 1)


@pytest.mark.parametrize(
    ("jobs", "files", "workers"),
    [
        (8, 0, 1),
        (8, 199, 1),
        (8, 200, 2),
        (8, 799, 7),
        (8, 800, 8),
        (8, 99_999, 8),
        (1, 99_999, 1),
        (2, 99_999, 2),
        (500, 99_999, 61),
    ],
)
def test_worker_count(jobs, files, workers):
    """One worker per 100 files, never more than --jobs or 61, and none under 200 files."""
    assert worker_count(jobs, files) == workers


def test_command_line_with_real_workers(tmp_path, monkeypatch, capsys):
    """200 files is where workers start: the report is the same, and nothing is warned."""
    for n in range(200):
        (tmp_path / f"m{n:03}.py").write_text(
            f"# I hope this helps!\ndef helper_{n}(value):\n"
            "    text = str(value)\n    text = text.strip()\n    return text\n",
            encoding="utf-8",
        )
    pools = []
    real = analysis._analyze_in_pool
    monkeypatch.setattr(analysis, "_analyze_in_pool", lambda *a: pools.append(a[2]) or real(*a))
    reports = []
    for jobs in ("1", "2", "8"):
        assert main([str(tmp_path), "--offline", "--format", "json", "--jobs", jobs]) == 1
        captured = capsys.readouterr()
        assert captured.err == ""
        reports.append(captured.out)
    assert pools == [2, 2]  # --jobs 8 still means one worker per 100 files
    assert reports[0] == reports[1] == reports[2]
    rules = [f["rule"] for f in json.loads(reports[0])["findings"]]
    assert (rules.count("SLOP051"), rules.count("SLOP030")) == (200, 199)


# Starts slopfence the way an installed ``slopfence`` command does: from a script in
# a folder of its own, so the current folder is not on the import path of the main
# process. Two files are enough for workers here, and they are started with "spawn".
CONSOLE_SCRIPT = """
import multiprocessing
import sys

sys.path.append(sys.argv[1])  # where slopfence is, if it is not installed
from slopfence import analysis
from slopfence.cli import main

if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")
    analysis.MIN_FILES_PER_WORKER = 2
    sys.exit(main(sys.argv[2:]))
"""


# A project file that is named like a standard module. Importing it leaves a file
# named "imported" next to it, and fails.
SHADOW = (
    "import os\n"
    "open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'imported'), 'a').close()\n"
    "raise ImportError('a project file was imported in place of a standard module')\n"
)


def shadowed_project(tmp_path, names, body=SHADOW):
    """A project of six files with one finding each, plus a file for each of ``names``."""
    project = tmp_path / "project"
    project.mkdir()
    for name in names:
        (project / f"{name}.py").write_text(body, encoding="utf-8")
    for n in range(6):
        (project / f"m{n}.py").write_text("# I hope this helps!\n", encoding="utf-8")
    return project


def run_in(project, command, jobs):
    """Run a slopfence command in the project folder; return its exit code, its JSON
    report and what it printed on stderr."""
    options = [".", "--offline", "--format", "json", "--jobs", jobs, "--select", "SLOP051"]
    done = subprocess.run([*command, *options], cwd=project, capture_output=True, text=True)
    return done.returncode, done.stdout, done.stderr


def console_script(tmp_path):
    """The command that starts slopfence like the installed script, with "spawn" workers."""
    script = tmp_path / "bin" / "run.py"
    script.parent.mkdir()
    script.write_text(CONSOLE_SCRIPT, encoding="utf-8")
    return [sys.executable, str(script), str(Path(analysis.__file__).parent.parent)]


@pytest.mark.skipif(sys.version_info < (3, 11), reason="PYTHONSAFEPATH is new in Python 3.11")
def test_project_file_named_like_a_standard_module_does_not_break_workers(tmp_path):
    """Run in a folder with a ``types.py``, as many projects have: the workers must not
    import it in place of the standard library's (they would die while starting)."""
    project = shadowed_project(tmp_path, ["types"], body="X = 1\n")
    command = console_script(tmp_path)
    code, report, warnings = run_in(project, command, "2")
    assert warnings == ""
    assert (code, report) == run_in(project, command, "1")[:2]
    assert [f["rule"] for f in json.loads(report)["findings"]] == ["SLOP051"] * 6


def test_workers_never_run_a_file_of_the_checked_project(tmp_path):
    """A new Python process looks for modules in the current folder first. A project
    file named like a module that Python loads while a worker starts must not be
    imported. On Python 3.10 that can't be switched off, so no worker is started."""
    project = shadowed_project(tmp_path, ["threading", "types", "selectors"])
    code, report, warnings = run_in(project, console_script(tmp_path), "2")
    assert not (project / "imported").exists()
    assert code == 1
    assert [f["rule"] for f in json.loads(report)["findings"]] == ["SLOP051"] * 6
    if sys.version_info >= (3, 11):
        assert warnings == ""
    else:
        assert "can't keep worker processes from importing project files" in warnings


@pytest.mark.skipif(sys.version_info < (3, 11), reason="without -E, Python 3.10 is covered above")
def test_workers_are_not_started_when_python_ignores_its_environment(tmp_path):
    """``python -E`` hands -E on to every worker, which then ignores PYTHONSAFEPATH too.
    So with -E no worker is started, unless they are forked."""
    project = shadowed_project(tmp_path, ["threading", "types", "selectors"])
    command = console_script(tmp_path)
    code, report, warnings = run_in(project, [command[0], "-E", *command[1:]], "2")
    assert not (project / "imported").exists()
    assert code == 1
    assert [f["rule"] for f in json.loads(report)["findings"]] == ["SLOP051"] * 6
    assert "can't keep worker processes from importing project files" in warnings


def test_python_m_never_runs_a_file_of_the_checked_project(tmp_path):
    """``python -m slopfence`` starts with the current folder first on the import
    path. Files there that are named like modules slopfence imports must not run."""
    names = ["__future__", "argparse", "fnmatch", "random", "tempfile", "tomllib", "json"]
    project = shadowed_project(tmp_path, names)
    code, report, warnings = run_in(project, [sys.executable, "-m", "slopfence"], "1")
    assert not (project / "imported").exists()
    assert (code, warnings) == (1, "")
    assert [f["rule"] for f in json.loads(report)["findings"]] == ["SLOP051"] * 6


@pytest.mark.parametrize(("first", "dropped"), [("cwd", True), ("", True), ("other", False)])
def test_module_run_drops_only_the_current_folder(tmp_path, monkeypatch, first, dropped):
    """The first entry of the import path goes when it is the folder slopfence was
    started in, written out or as an empty string. Any other entry stays."""
    entry = importlib.import_module("slopfence.__main__")
    (tmp_path / "other").mkdir()
    monkeypatch.chdir(tmp_path)
    head = {"cwd": str(tmp_path), "": "", "other": str(tmp_path / "other")}[first]
    monkeypatch.setattr(sys, "path", [head, *sys.path])
    before = list(sys.path)
    entry._leave_out_the_current_folder()
    assert sys.path == (before[1:] if dropped else before)


def test_module_run_keeps_the_folder_slopfence_itself_is_in(monkeypatch):
    """Started in a source checkout that is not installed, slopfence is only found
    through the current folder: that entry must stay."""
    entry = importlib.import_module("slopfence.__main__")
    home = Path(entry.__file__).resolve().parent.parent
    monkeypatch.chdir(home)
    monkeypatch.setattr(sys, "path", [str(home), *sys.path])
    before = list(sys.path)
    entry._leave_out_the_current_folder()
    assert sys.path == before


def test_import_path_is_left_alone_when_the_module_is_only_imported(tmp_path, monkeypatch):
    """A worker process imports ``slopfence.__main__`` again, under another name. That
    must not touch its import path: only a real ``python -m slopfence`` does."""
    entry = importlib.import_module("slopfence.__main__")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    before = list(sys.path)
    importlib.reload(entry)
    assert sys.path == before


def test_module_run_with_an_empty_import_path(monkeypatch):
    """An import path without entries has nothing to drop."""
    entry = importlib.import_module("slopfence.__main__")
    monkeypatch.setattr(sys, "path", [])
    entry._leave_out_the_current_folder()
    assert sys.path == []


def python_like(version, **flags):
    """A stand-in for ``sys`` as a Python of that version sees it, started with ``flags``.
    Python 3.10 has no ``safe_path`` flag."""
    known = {"isolated": 0, "ignore_environment": 0}
    if version >= (3, 11):
        known["safe_path"] = False
    return types.SimpleNamespace(
        version_info=version, stderr=sys.stderr, flags=types.SimpleNamespace(**{**known, **flags})
    )


@pytest.mark.parametrize(
    ("version", "flags", "any_method"),
    [
        ((3, 10, 12), {}, False),  # no PYTHONSAFEPATH before 3.11
        ((3, 10, 12), {"isolated": 1, "ignore_environment": 1}, True),  # -I goes to the workers
        ((3, 12, 3), {}, True),  # PYTHONSAFEPATH
        ((3, 12, 3), {"ignore_environment": 1}, False),  # -E: the variable is ignored
        ((3, 12, 3), {"ignore_environment": 1, "safe_path": True}, True),  # -E -P
        ((3, 14, 0), {"isolated": 1, "ignore_environment": 1, "safe_path": True}, True),  # -I
    ],
)
def test_which_pythons_may_start_new_interpreters(monkeypatch, version, flags, any_method):
    """A worker that is a new interpreter needs something that keeps the current
    folder off its import path: -I or -P on this Python, or PYTHONSAFEPATH where it
    is read. Without one of these only forked workers are safe."""
    monkeypatch.setattr(analysis, "sys", python_like(version, **flags))
    for method in multiprocessing.get_all_start_methods():
        context = multiprocessing.get_context(method)
        assert analysis._new_processes_are_safe(context) is (any_method or method == "fork")
    default_is_fork = multiprocessing.get_start_method() == "fork"
    assert analysis._new_processes_are_safe(None) is (any_method or default_is_fork)


@pytest.mark.parametrize(
    "method", [m for m in multiprocessing.get_all_start_methods() if m != "fork"]
)
def test_pool_gives_up_before_it_starts_an_unsafe_process(project, monkeypatch, capsys, method):
    """Where a new interpreter would not be safe, the pool answers None with a warning
    and starts nothing: the files are then checked in one process."""
    monkeypatch.setattr(analysis, "sys", python_like((3, 10, 12)))
    tasks, options = tasks_and_options(project)
    context = multiprocessing.get_context(method)
    assert analysis._analyze_in_pool(tasks, options, 2, context) is None
    assert "can't keep worker processes from importing project files" in capsys.readouterr().err


@pytest.mark.parametrize("before", [None, "", "yes"])
def test_safe_import_path_is_only_set_while_processes_start(monkeypatch, before):
    """The setting is for the processes started in the block. Afterwards the
    environment is what it was, whether the variable was set or not."""
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    if before is not None:
        monkeypatch.setenv("PYTHONSAFEPATH", before)
    with analysis._safe_import_path():
        assert os.environ["PYTHONSAFEPATH"] == "1"
    assert os.environ.get("PYTHONSAFEPATH") == before


def use_failing_pool(monkeypatch, error, fail_in_map):
    """Make every pool fail with ``error``: when it is created or, with ``fail_in_map``,
    when it is used. Returns the list of shutdown calls."""
    import concurrent.futures

    shutdowns: list[dict] = []

    class FailingPool:
        """Stands in for ProcessPoolExecutor."""

        def __init__(self, **kwargs):
            """Accept the real pool's arguments; maybe fail like a platform without pools."""
            if not fail_in_map:
                raise error

        def map(self, *args, **kwargs):
            """Fail while results are collected."""
            raise error

        def shutdown(self, **kwargs):
            """Record how the pool was shut down."""
            shutdowns.append(kwargs)

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", FailingPool)
    monkeypatch.setattr(analysis, "MIN_FILES_PER_WORKER", 2)
    return shutdowns


@pytest.mark.parametrize(
    ("error", "fail_in_map"),
    [
        (OSError("no semaphores here"), False),
        (ImportError("no _multiprocessing"), False),
        (NotImplementedError("no sem_open"), False),
        (OSError("no more processes"), True),
        (EOFError("unexpected EOF"), True),  # the fork server died while it started
        (BrokenProcessPool("a worker died"), True),
    ],
)
def test_falls_back_to_one_process(project, monkeypatch, capsys, error, fail_in_map):
    """If workers can't be started, or one dies, the files are checked in this process."""
    serial = scan(project, 1)
    shutdowns = use_failing_pool(monkeypatch, error, fail_in_map)
    assert scan(project, 4) == serial
    assert capsys.readouterr().err == (
        f"slopfence: warning: worker processes failed ({type(error).__name__}: {error}); "
        "checking files in one process instead\n"
    )
    # A pool that was created is always shut down, and never left with queued work.
    assert shutdowns == ([{"wait": True, "cancel_futures": True}] if fail_in_map else [])


def _exit_at_once():
    """A worker initializer that kills the worker (workers must be able to import it)."""
    os._exit(3)


def test_dead_worker_is_survived(project, monkeypatch, capfd):
    """A worker that dies breaks the pool; the run still finishes with the same result."""
    tasks, options = tasks_and_options(project)
    serial = analyze_files(tasks, options, 1)
    monkeypatch.setattr(analysis, "MIN_FILES_PER_WORKER", 2)
    monkeypatch.setattr(analysis, "_init_worker", _exit_at_once)
    assert analyze_files(tasks, options, 2) == serial
    assert "worker processes failed (BrokenProcessPool" in capfd.readouterr().err


def test_error_in_a_file_is_raised_like_with_one_job(project, monkeypatch, capfd):
    """A crash while a worker analyses a file is raised as it is with one job. It is
    not mistaken for a broken pool, so there is no warning and no second attempt."""
    tasks, options = tasks_and_options(project)
    tasks.append(("no\0such.py", True))  # no file name may hold a null character
    with pytest.raises(ValueError, match="null") as serial:
        analyze_files(tasks, options, 1)
    pools = count_pools(monkeypatch)
    with pytest.raises(ValueError, match="null") as parallel:
        analyze_files(tasks, options, 2)
    assert pools == [2]
    assert str(parallel.value) == str(serial.value)
    assert "worker processes failed" not in capfd.readouterr().err


def test_worker_ignores_ctrl_c_and_watches_the_main_process(monkeypatch):
    """Only the main process reacts to Ctrl-C, and every worker waits for its end."""
    calls, watching = [], threading.Event()
    monkeypatch.setattr(signal, "signal", lambda sig, handler: calls.append((sig, handler)))
    monkeypatch.setattr(analysis, "_end_with_parent", watching.set)
    analysis._init_worker()
    assert calls == [(signal.SIGINT, signal.SIG_IGN)]
    assert watching.wait(timeout=10)


def test_worker_ends_once_the_main_process_is_gone(monkeypatch):
    """The watcher waits until the main process has ended, and only then ends the worker."""
    events = []

    class GoneParent:
        """Stands in for a main process that has ended."""

        def join(self):
            """Return at once: there is nothing left to wait for."""
            events.append("waited")

    monkeypatch.setattr(multiprocessing, "parent_process", GoneParent)
    monkeypatch.setattr(os, "_exit", lambda status: events.append(status))
    analysis._end_with_parent()
    assert events == ["waited", 1]


def _heartbeat(folder, number):
    """A task that keeps a worker busy: its file grows for as long as the worker lives
    (half a minute at most, so that a failed test leaves nothing behind for long)."""
    beat, stop = Path(folder, f"beat-{number}"), Path(folder, "stop")
    for _ in range(1500):
        if stop.exists():
            break
        with beat.open("a", encoding="utf-8") as file:
            file.write(".")
        time.sleep(0.02)


def _own_a_pool(folder, method):
    """Stand in for the main process: start two busy workers, then wait to be killed."""
    from concurrent.futures import ProcessPoolExecutor

    pool = ProcessPoolExecutor(
        2, mp_context=multiprocessing.get_context(method), initializer=analysis._init_worker
    )
    for number in range(2):
        pool.submit(_heartbeat, folder, number)
    time.sleep(30)


@pytest.mark.parametrize("method", multiprocessing.get_all_start_methods())
def test_workers_end_when_the_main_process_is_killed(tmp_path, method):
    """A main process that is killed can't tell its workers to stop. They notice, and
    end by themselves, instead of staying behind for good."""
    code = "import sys; from tests.test_parallel import _own_a_pool; _own_a_pool(*sys.argv[1:])"
    owner = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), method], cwd=Path(__file__).parent.parent
    )
    beats = [tmp_path / "beat-0", tmp_path / "beat-1"]

    def stopped():
        """Whether the workers write nothing: two readings half a second apart are equal."""
        before = [beat.stat().st_size for beat in beats]
        time.sleep(0.5)
        return [beat.stat().st_size for beat in beats] == before

    try:
        deadline = time.monotonic() + 60
        while not all(beat.exists() for beat in beats):
            assert owner.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        owner.kill()
        owner.wait()
        deadline = time.monotonic() + 10
        while not stopped() and time.monotonic() < deadline:
            continue
        assert stopped()
    finally:
        owner.kill()
        (tmp_path / "stop").touch()


needs_signal_mask = pytest.mark.skipif(
    not hasattr(signal, "pthread_sigmask"), reason="Windows can't hold a signal back"
)


def _ctrl_c_is_held_back(options, task):
    """Stands in for ``analyze_file`` in a worker: is Ctrl-C held back in this process?"""
    return signal.SIGINT in signal.pthread_sigmask(signal.SIG_BLOCK, [])


@needs_signal_mask
@pytest.mark.parametrize("method", POOL_METHODS)
def test_workers_start_with_ctrl_c_held_back(project, monkeypatch, method):
    """A worker can't be interrupted while it starts up (it would print a traceback):
    Ctrl-C is held back in every worker from its first moment, and not in this process."""
    tasks, options = tasks_and_options(project)
    monkeypatch.setattr(analysis, "analyze_file", _ctrl_c_is_held_back)
    context = multiprocessing.get_context(method)
    assert analysis._analyze_in_pool(tasks, options, 3, context) == [True] * len(tasks)
    assert not _ctrl_c_is_held_back(options, tasks[0])


@needs_signal_mask
def test_ctrl_c_arrives_when_the_block_is_left():
    """Ctrl-C while workers are started is not lost: it arrives right after."""
    seen = []
    previous = signal.signal(signal.SIGINT, lambda *args: seen.append("interrupt"))
    try:
        with analysis._ctrl_c_held_back():
            signal.raise_signal(signal.SIGINT)
            seen.append("still in the block")
        assert seen == ["still in the block", "interrupt"]
    finally:
        signal.signal(signal.SIGINT, previous)


def test_nothing_is_held_back_where_signals_cannot_be(monkeypatch):
    """On Windows there is no signal mask: the block runs, and changes nothing."""
    monkeypatch.delattr(signal, "pthread_sigmask", raising=False)
    seen = []
    previous = signal.signal(signal.SIGINT, lambda *args: seen.append("interrupt"))
    try:
        with analysis._ctrl_c_held_back():
            signal.raise_signal(signal.SIGINT)
            seen.append("still in the block")
        assert seen == ["interrupt", "still in the block"]
    finally:
        signal.signal(signal.SIGINT, previous)


def test_ctrl_c_drops_queued_work(project, monkeypatch, capsys):
    """Ctrl-C while workers run: queued batches are dropped and the interrupt goes on."""
    tasks, options = tasks_and_options(project)
    shutdowns = use_failing_pool(monkeypatch, KeyboardInterrupt(), fail_in_map=True)
    with pytest.raises(KeyboardInterrupt):
        analyze_files(tasks, options, 2)
    assert shutdowns == [{"wait": True, "cancel_futures": True}]
    assert capsys.readouterr().err == ""


def test_interrupt_exits_130(tmp_path, monkeypatch, capsys):
    """Ctrl-C ends the run with one line, not a traceback. Then the interrupt is sent
    again with its default action; where that can't be done, the exit status is 130."""

    def interrupted(*args, **kwargs):
        """Stand in for a run that is interrupted."""
        raise KeyboardInterrupt

    sent = []
    monkeypatch.setattr(climod, "run", interrupted)
    monkeypatch.setattr(signal, "signal", lambda *args: sent.append(args))
    monkeypatch.setattr(signal, "raise_signal", lambda *args: sent.append(args))
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    output = tmp_path / "report.json"
    assert main([str(tmp_path), "--offline", "--exit-zero", "-o", str(output)]) == 130
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "slopfence: interrupted\n")
    assert not output.exists()  # no report, not even an empty one
    again = [(signal.SIGINT, signal.SIG_DFL), (signal.SIGINT,)]
    assert sent == (again if os.name == "posix" else [])


@pytest.mark.skipif(os.name != "posix", reason="Windows can't send Ctrl-C to one process")
def test_ctrl_c_ends_the_process_like_any_interrupted_program(tmp_path):
    """A real Ctrl-C: the process dies of the interrupt, as v0.2 did. That is what makes
    a shell stop the script or loop that runs slopfence; an exit status alone doesn't."""
    code = (
        "import signal, sys, time\n"
        "from slopfence import cli\n"
        "def run(*args, **kwargs):\n"
        "    print('running', flush=True)\n"
        "    time.sleep(60)\n"
        "cli.run = run\n"
        # The interrupt may be switched off in the process that runs the tests.
        "signal.signal(signal.SIGINT, signal.default_int_handler)\n"
        "sys.exit(cli.main([sys.argv[1], '--offline']))\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout.readline() == "running\n"
    process.send_signal(signal.SIGINT)
    out, err = process.communicate(timeout=30)
    assert process.returncode == -signal.SIGINT
    assert (out, err) == ("", "slopfence: interrupted\n")


def test_default_jobs(monkeypatch):
    """One job per CPU the process may use, at most 8, whichever way Python reports CPUs."""
    assert 1 <= default_jobs() <= 8
    monkeypatch.setattr(os, "process_cpu_count", lambda: 64, raising=False)
    assert default_jobs() == 8
    monkeypatch.setattr(os, "process_cpu_count", lambda: None, raising=False)
    assert default_jobs() == 1
    monkeypatch.delattr(os, "process_cpu_count")
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1, 2}, raising=False)
    assert default_jobs() == 3
    monkeypatch.delattr(os, "sched_getaffinity")
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    assert default_jobs() == 4
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert default_jobs() == 1


def test_jobs_option_and_config(tmp_path, monkeypatch, capsys):
    """--jobs replaces the config value; with neither, the default is used. The report
    is the same every time."""
    (tmp_path / "app.py").write_text("# I hope this helps!\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.slopfence]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    seen, reports = [], []
    real = climod.run
    monkeypatch.setattr(climod, "run", lambda *a, **k: seen.append(k["jobs"]) or real(*a, **k))

    def scan_with(*options):
        """Run the CLI and keep its JSON report."""
        main([".", "--offline", "--format", "json", *options])
        reports.append(capsys.readouterr().out)

    scan_with()
    scan_with("--jobs", "3")
    (tmp_path / "pyproject.toml").write_text("[tool.slopfence]\njobs = 2\n", encoding="utf-8")
    scan_with()
    scan_with("--jobs", "1")
    assert seen == [default_jobs(), 3, 2, 1]
    assert len(set(reports)) == 1
    assert json.loads(reports[0])["findings"][0]["rule"] == "SLOP051"


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "many", ""])
def test_invalid_jobs_option(tmp_path, value, capsys):
    """--jobs must be a whole number, 1 or more: anything else is a usage error."""
    with pytest.raises(SystemExit) as exit_info:
        main([str(tmp_path), "--jobs", value])
    assert exit_info.value.code == 2
    assert f"argument --jobs: must be a whole number, 1 or more (got {value!r})" in (
        capsys.readouterr().err
    )


@pytest.mark.parametrize("value", ["0", "-2", "true", '"4"', "1.5", "[2]"])
def test_invalid_jobs_config(tmp_path, value):
    """jobs in pyproject.toml must be a whole number, 1 or more."""
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.slopfence]\njobs = {value}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="'jobs' must be a whole number, 1 or more"):
        load_config(tmp_path)


def test_jobs_is_a_known_config_key(tmp_path):
    """``jobs`` is read from the config, and named among the keys a typo is told about."""
    (tmp_path / "pyproject.toml").write_text("[tool.slopfence]\njobs = 16\n", encoding="utf-8")
    assert load_config(tmp_path).jobs == 16
    (tmp_path / "pyproject.toml").write_text("[tool.slopfence]\njob = 16\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"unknown key\(s\): job \(supported: .*\bjobs\b"):
        load_config(tmp_path)


def test_main_module_can_be_imported_without_running():
    """A worker process may import ``slopfence.__main__``; that must not start a run."""
    entry = importlib.import_module("slopfence.__main__")
    assert entry.main is main


def test_starting_up_stays_cheap():
    """Worker processes import slopfence.analysis: it must not pull in the registry.
    And a run that starts no workers must not import the pool machinery."""
    code = (
        "import sys, slopfence.analysis\n"
        "assert 'slopfence.registry' not in sys.modules\n"
        "import slopfence.cli\n"
        "assert 'concurrent.futures' not in sys.modules\n"
        "assert 'multiprocessing' not in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
