"""What slopfence learns from one Python file, in a form that can cross processes.

``analyze_file`` is the unit of work. It parses one file, runs the rules that need
nothing but that file, and returns a ``FileSummary``: the findings, plus the few
facts the project-wide rules need (function fingerprints for SLOP030, imports for
SLOP001). The syntax tree never leaves the function, so no two trees are in memory
at once. The summaries are kept until the run ends: memory still grows with the
project, but by a small summary per file and not by a tree per file.

``analyze_files`` runs it for many files, in this process or in worker processes.
Workers import this module. Keep it cheap to import, and never import the registry
here: a worker must not be able to reach the network.
"""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from slopfence.detectors import FILE_DETECTORS
from slopfence.detectors.duplicates import Function, index_functions
from slopfence.models import Finding
from slopfence.source import is_test_path, load, relative_path, top_level_imports

if TYPE_CHECKING:
    from multiprocessing.context import BaseContext

# The default number of jobs is one per CPU, but never more than this. A worker needs
# 40 to 80 MB on big projects, and a container's memory limit doesn't show in its CPU
# count.
DEFAULT_MAX_JOBS = 8
# One worker is started for every this many files, so none below twice this number.
# Measured on Linux, workers already pay off with half as many files each, whatever
# the start method. The margin is for Windows and macOS, where starting a process
# costs more.
MIN_FILES_PER_WORKER = 100
# Windows can't wait on more than 61 processes.
MAX_WORKERS = 61
# Most files sent to a worker in one message. Bigger batches don't make messaging
# measurably cheaper; they make workers finish unevenly and Ctrl-C slower.
MAX_CHUNK = 32

# One file to analyse: (path, check it). With False the file is not checked. Only its
# functions are fingerprinted, so that SLOP030 can compare checked files with it.
Task = tuple[str, bool]


@dataclass(frozen=True)
class Options:
    """What to do with each file. Small and picklable: it is sent with every batch."""

    root: str
    # Per-file rules to run (keys of FILE_DETECTORS).
    rules: frozenset[str]
    # Fingerprint functions, for SLOP030.
    functions: bool = False
    # Collect imports, for SLOP001 with check-imports.
    imports: bool = False


@dataclass(frozen=True)
class FileSummary:
    """Everything the engine needs from one file once its syntax tree is gone."""

    rel: str
    # False when the file could not be read or parsed.
    parsed: bool = True
    # What the per-file rules found. Ignore comments are not applied yet.
    findings: list[Finding] = field(default_factory=list)
    functions: list[Function] = field(default_factory=list)
    # (top-level module, line, column) of every absolute import, in ast.walk order.
    imports: list[tuple[str, int, int]] = field(default_factory=list)
    # The file's ignore comments, as in ``SourceFile``.
    ignore_file: bool = False
    ignores: dict[int, set[str] | None] = field(default_factory=dict)

    def is_ignored(self, rule: str, line: int) -> bool:
        """Whether an ignore comment in the file covers ``rule`` on ``line``."""
        if self.ignore_file:
            return True
        if line not in self.ignores:
            return False
        rules = self.ignores[line]
        return rules is None or rule in rules


def analyze_file(options: Options, task: Task) -> FileSummary:
    """Parse one file and run everything that needs nothing but that file.

    Runs in a worker process or in the main one. So it is a module-level function,
    it takes and returns plain data that can be pickled, and it prints nothing.
    """
    path_text, check = task
    path, root = Path(path_text), Path(options.root)
    if not check:
        # Only here to be compared with. Test code has no fingerprints, so it isn't read.
        rel = relative_path(path, root)
        if is_test_path(path.name, rel):
            return FileSummary(rel=rel)
    src = load(path, root, comments=check)
    if src is None:
        return FileSummary(rel=relative_path(path, root), parsed=False)
    findings: list[Finding] = []
    if check:
        for rule, detector in FILE_DETECTORS.items():
            if rule in options.rules:
                findings.extend(detector(src))
    return FileSummary(
        rel=src.rel,
        findings=findings,
        functions=index_functions(src) if options.functions else [],
        imports=top_level_imports(src) if check and options.imports else [],
        ignore_file=src.ignore_file,
        ignores=src.ignores,
    )


def default_jobs() -> int:
    """Jobs to use when the user didn't say: one per CPU this process may use, at most 8."""
    counter = getattr(os, "process_cpu_count", None)  # Python 3.13 and later
    if counter is not None:
        cpus = counter()
    elif hasattr(os, "sched_getaffinity"):  # Linux: respects taskset and a container's CPU set
        cpus = len(os.sched_getaffinity(0))
    else:
        cpus = os.cpu_count()
    return max(1, min(cpus or 1, DEFAULT_MAX_JOBS))


def worker_count(jobs: int, files: int) -> int:
    """How many worker processes to use for ``files`` files. 1 means none: stay in-process."""
    return max(1, min(jobs, MAX_WORKERS, files // MIN_FILES_PER_WORKER))


def _init_worker() -> None:
    """Set up a worker process: it leaves Ctrl-C to the main process, and ends with it."""
    # Imported here: only a worker process needs it.
    import threading

    # Otherwise every worker would print a traceback of its own.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    threading.Thread(target=_end_with_parent, daemon=True).start()


def _end_with_parent() -> None:
    """End this worker process once the main process is gone.

    A worker waits for its next batch for ever. A main process that is killed (``kill``,
    a closed terminal, the out-of-memory killer) can't tell it to stop, so the worker
    watches for that itself. Otherwise it would stay behind, holding its memory.
    """
    import multiprocessing

    multiprocessing.parent_process().join()
    os._exit(1)


@contextmanager
def _ctrl_c_held_back() -> Iterator[None]:
    """Hold Ctrl-C back for a moment. It is not lost: it arrives when the block is left.

    A process that is started inside the block starts with Ctrl-C held back too, and
    there it stays that way. A worker that is interrupted while it starts up prints a
    traceback, and this is the only way to rule that out from its first moment.

    Windows can't hold a signal back. There the block changes nothing.
    """
    set_mask = getattr(signal, "pthread_sigmask", None)
    before = set_mask(signal.SIG_BLOCK, {signal.SIGINT}) if set_mask else None
    try:
        yield
    finally:
        if set_mask:
            set_mask(signal.SIG_SETMASK, before)


@contextmanager
def _safe_import_path() -> Iterator[None]:
    """Processes started in this block don't look for modules in the current folder.

    Python starts a worker with the current folder first on its import path. A project
    file named like a standard-library module (``types.py``, ``random.py``) is then
    imported in its place, and the worker dies before this process has given it the
    import path to use. ``PYTHONSAFEPATH`` switches that first entry off.

    Python reads the variable from 3.11 on, and not when it was started with ``-E``.
    Where it is not read, no new interpreter is started at all (see
    ``_new_processes_are_safe``).
    """
    before = os.environ.get("PYTHONSAFEPATH")
    os.environ["PYTHONSAFEPATH"] = "1"
    try:
        yield
    finally:
        if before is None:
            del os.environ["PYTHONSAFEPATH"]
        else:
            os.environ["PYTHONSAFEPATH"] = before


def _new_processes_are_safe(mp_context: BaseContext | None) -> bool:
    """Whether a worker can start without importing files of the checked project.

    A new Python process looks for modules in the current folder first, and that
    folder is the project. A project file named like a module that Python loads while
    it starts (``threading.py``) would be imported, and so run. Three things prevent it:

    * This Python was started with ``-I`` or ``-P``: Python starts its children with
      the same option.
    * ``_safe_import_path``, which works from Python 3.11 on, unless this Python was
      started with ``-E`` and so ignores the variable.
    * The "fork" start method: a forked worker is a copy of this process, not a new
      interpreter.
    """
    flags = sys.flags
    if flags.isolated or getattr(flags, "safe_path", False):
        return True
    if sys.version_info >= (3, 11) and not flags.ignore_environment:
        return True
    # Imported here: a run that starts no workers doesn't pay for it.
    import multiprocessing

    return (mp_context or multiprocessing).get_start_method() == "fork"


def _analyze_in_pool(
    tasks: Sequence[Task], options: Options, workers: int, mp_context: BaseContext | None
) -> list[FileSummary] | None:
    """``analyze_file`` for every task in ``workers`` processes; results in task order.

    Returns None, after a warning on stderr, when worker processes can't be used
    here or one of them died. The caller then does the work in this process.
    """
    if not _new_processes_are_safe(mp_context):
        print(
            "slopfence: warning: this Python can't keep worker processes from importing "
            "project files (Python 3.10, or -E without -I); checking files in one process "
            "instead",
            file=sys.stderr,
        )
        return None
    # Imported here: a run that starts no workers doesn't pay for it.
    from concurrent.futures import BrokenExecutor

    chunk = max(1, min(MAX_CHUNK, len(tasks) // (workers * 4)))
    try:
        # Not every platform has process pools: the import or the constructor fails there.
        from concurrent.futures import ProcessPoolExecutor

        with ExitStack() as cleanup:
            # Every process is started in this block: a helper process (the resource
            # tracker) when the pool is made, and the workers by map(), which hands out
            # all the batches before it returns.
            with _safe_import_path():
                pool = ProcessPoolExecutor(
                    max_workers=workers, mp_context=mp_context, initializer=_init_worker
                )
                # Also after Ctrl-C or an error: batches that have not started are
                # dropped, and the ones that are running finish first, which takes a
                # moment.
                cleanup.callback(pool.shutdown, wait=True, cancel_futures=True)
                # Not around the pool's constructor: the helper looks after itself, and
                # before Python 3.14 starting it lets Ctrl-C through again.
                with _ctrl_c_held_back():
                    results = pool.map(partial(analyze_file, options), tasks, chunksize=chunk)
            return list(results)
    # BrokenExecutor: a worker died. EOFError: the fork server died while it started.
    # The others: this platform has no process pools, or no room to start one. An
    # error of these types from analyze_file itself lands here too; the single-process
    # run then raises it again.
    except (BrokenExecutor, EOFError, OSError, ImportError, NotImplementedError) as err:
        print(
            f"slopfence: warning: worker processes failed ({type(err).__name__}: {err}); "
            "checking files in one process instead",
            file=sys.stderr,
        )
        return None


def analyze_files(
    tasks: Sequence[Task],
    options: Options,
    jobs: int = 1,
    mp_context: BaseContext | None = None,
) -> list[FileSummary]:
    """One ``FileSummary`` per task, in task order, using up to ``jobs`` processes.

    The result is the same for any number of jobs. Worker processes are only started
    when there are enough files (see ``worker_count``). If they can't be started, or
    one dies, every file is analysed in this process instead.

    ``mp_context`` picks the start method ("spawn", "fork"...). The default is the
    platform's own; the tests use the others.
    """
    workers = worker_count(jobs, len(tasks))
    if workers > 1:
        summaries = _analyze_in_pool(tasks, options, workers, mp_context)
        if summaries is not None:
            return summaries
    return [analyze_file(options, task) for task in tasks]
