"""What slopfence learns from one Python file, in a form that can cross processes.

``analyze_file`` is the unit of work. It parses one file, runs the rules that need
nothing but that file, and returns a ``FileSummary``: the findings, plus the few
facts the project-wide rules need (function fingerprints for SLOP030, imports for
SLOP001). The syntax tree never leaves the function, so memory stays flat however
big the project is.

``analyze_files`` runs it for many files, in this process or in worker processes.
Workers import this module. Keep it cheap to import, and never import the registry
here: a worker must not be able to reach the network.
"""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from slopfence.detectors import FILE_DETECTORS
from slopfence.detectors.duplicates import Function, index_functions
from slopfence.models import Finding
from slopfence.source import ignore_covers, is_test_path, load, relative_path, top_level_imports

if TYPE_CHECKING:
    from multiprocessing.context import BaseContext

# The default number of jobs is one per CPU, but never more than this. A worker needs
# about 60 MB, and a container's memory limit doesn't show in its CPU count.
DEFAULT_MAX_JOBS = 8
# One worker is started for every this many files, so none below twice this number.
# Starting a process costs more than it saves for fewer files: about 45 on Linux with
# the "spawn" start method, doubled here for Windows and macOS, where it is slower.
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
        return ignore_covers(self.ignore_file, self.ignores, rule, line)


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
    """Leave Ctrl-C to the main process, so that workers don't each print a traceback."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _analyze_in_pool(
    tasks: Sequence[Task], options: Options, workers: int, mp_context: BaseContext | None
) -> list[FileSummary] | None:
    """``analyze_file`` for every task in ``workers`` processes; results in task order.

    Returns None, after a warning on stderr, when worker processes can't be used
    here or one of them died. The caller then does the work in this process.
    """
    # Imported here: a run that starts no workers doesn't pay for it.
    from concurrent.futures import BrokenExecutor

    chunk = max(1, min(MAX_CHUNK, len(tasks) // (workers * 4)))
    try:
        # Not every platform has process pools: the import or the constructor fails there.
        from concurrent.futures import ProcessPoolExecutor

        pool = ProcessPoolExecutor(
            max_workers=workers, mp_context=mp_context, initializer=_init_worker
        )
        try:
            return list(pool.map(partial(analyze_file, options), tasks, chunksize=chunk))
        finally:
            # Also after Ctrl-C or an error: batches that have not started are dropped,
            # and the ones that are running finish first, which takes a moment.
            pool.shutdown(wait=True, cancel_futures=True)
    # BrokenExecutor: a worker died. The others: this platform has no process pools,
    # or no room to start one. An OSError from analyze_file itself lands here too;
    # the single-process run then raises it again.
    except (BrokenExecutor, OSError, ImportError, NotImplementedError) as err:
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
