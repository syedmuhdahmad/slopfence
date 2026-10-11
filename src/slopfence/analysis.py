"""What slopfence learns from one Python file.

``analyze_file`` is the unit of work. It parses one file, runs the rules that need
nothing but that file, and returns a ``FileSummary``: the findings, plus the few
facts the project-wide rules need (function fingerprints for SLOP030, imports for
SLOP001). The syntax tree never leaves the function, so memory stays flat however
big the project is.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from slopfence.detectors import FILE_DETECTORS
from slopfence.detectors.duplicates import Function, index_functions
from slopfence.models import Finding
from slopfence.source import ignore_covers, is_test_path, load, relative_path, top_level_imports

# One file to analyse: (path, check it). With False the file is not checked. Only its
# functions are fingerprinted, so that SLOP030 can compare checked files with it.
Task = tuple[str, bool]


@dataclass(frozen=True)
class Options:
    """What to do with each file."""

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
    """Parse one file and run everything that needs nothing but that file."""
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


def analyze_files(tasks: Sequence[Task], options: Options) -> list[FileSummary]:
    """One ``FileSummary`` per task, in task order."""
    return [analyze_file(options, task) for task in tasks]
