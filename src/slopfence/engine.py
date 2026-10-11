"""File discovery and running detectors over a project."""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from slopfence import diff as diffmod
from slopfence.analysis import Options, Task, analyze_files
from slopfence.config import is_excluded
from slopfence.detectors import FILE_DETECTORS
from slopfence.detectors.duplicates import check_duplicates
from slopfence.detectors.packages import LOCK_FILES, PackageChecker, build_index
from slopfence.models import RULES, Finding
from slopfence.registry import Registry
from slopfence.source import relative_path as _rel_to

EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    ".env",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    ".tox",
    ".nox",
    ".eggs",
    "site-packages",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


def _is_dependency_file(path: Path) -> bool:
    name = path.name
    return name == "pyproject.toml" or (name.startswith("requirements") and name.endswith(".txt"))


def discover_all(paths: Sequence[Path]) -> tuple[list[Path], list[Path], list[Path]]:
    """Return (python files, dependency files, lock files) under the given paths."""
    py_files: list[Path] = []
    dep_files: list[Path] = []
    lock_files: list[Path] = []
    for path in paths:
        path = path.resolve()
        if path.is_file():
            if path.suffix == ".py":
                py_files.append(path)
            elif _is_dependency_file(path):
                dep_files.append(path)
            elif path.name in LOCK_FILES:
                lock_files.append(path)
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            # Prune excluded trees (.venv, node_modules, .git...) before descending.
            dirnames[:] = [
                d for d in dirnames if d not in EXCLUDED_DIRS and not d.endswith(".egg-info")
            ]
            for filename in filenames:
                child = Path(dirpath, filename)
                if child.suffix == ".py":
                    py_files.append(child)
                elif _is_dependency_file(child):
                    dep_files.append(child)
                elif filename in LOCK_FILES:
                    lock_files.append(child)
    return sorted(set(py_files)), sorted(set(dep_files)), sorted(set(lock_files))


def _not_excluded(files: list[Path], root: Path, exclude: Sequence[str]) -> list[Path]:
    """``files`` without the ones that match an ``exclude`` pattern."""
    if not exclude:
        return files  # the usual case, and no relative path has to be worked out
    return [p for p in files if not is_excluded(_rel_to(p, root), exclude)]


@dataclass
class Result:
    findings: list[Finding] = field(default_factory=list)
    files_checked: int = 0
    parse_errors: list[str] = field(default_factory=list)
    # Things the user should know about the run, e.g. why lookups were skipped.
    notes: list[str] = field(default_factory=list)


def run(
    paths: Sequence[Path],
    root: Path,
    registry: Registry,
    select: Iterable[str] | None = None,
    ignore: Iterable[str] = (),
    changed: diffmod.ChangedLines | None = None,
    exclude: Sequence[str] = (),
    known_packages: Sequence[str] = (),
    check_imports: bool = False,
    private_index: str | None = None,
    jobs: int = 1,
) -> Result:
    """Check ``paths`` and return the findings.

    SLOP001 always checks dependency files. Imports in source code are only looked
    up on PyPI with ``check_imports``, because that sends their names to pypi.org
    and can reveal private package names.

    ``private_index`` says where the environment configures a non-PyPI package
    index (see ``indexes.find_private_index``); SLOP001 then makes no lookups.

    Files are parsed and checked in up to ``jobs`` worker processes when there are
    enough of them (see ``analysis.analyze_files``). The result does not depend on
    ``jobs``. Registry lookups always happen in this process.
    """
    rules = set(select) if select else set(RULES)
    rules -= set(ignore)
    root = root.resolve()

    found_py, found_deps, found_locks = discover_all(paths)
    # Excluded files are not checked, but still count as project modules below.
    py_files = _not_excluded(found_py, root, exclude)
    dep_files = _not_excluded(found_deps, root, exclude)

    # SLOP030 and SLOP001's import check look at the whole project, not just the checked
    # paths: when pre-commit passes only changed files, their functions and imports are
    # still compared with the rest.
    project = (found_py, found_deps, found_locks)
    whole_project_given = any(Path(p).resolve() == root for p in paths)
    needs_project = "SLOP030" in rules or ("SLOP001" in rules and check_imports)
    if needs_project and not whole_project_given:
        project = discover_all([root])

    tasks: list[Task] = [(str(p), True) for p in py_files]
    if "SLOP030" in rules and not whole_project_given:
        # The rest of the project is only fingerprinted, to compare checked functions with.
        checked_paths = {p.resolve() for p in py_files}
        tasks += [
            (str(p), False)
            for p in _not_excluded(project[0], root, exclude)
            if p.resolve() not in checked_paths
        ]
    options = Options(
        root=str(root),
        rules=frozenset(rules & set(FILE_DETECTORS)),
        functions="SLOP030" in rules,
        imports="SLOP001" in rules and check_imports,
    )
    summaries = analyze_files(tasks, options, jobs)
    checked, compared_with = summaries[: len(py_files)], summaries[len(py_files) :]

    result = Result()
    result.parse_errors = [s.rel for s in checked if not s.parsed]
    sources = [s for s in checked if s.parsed]
    result.files_checked = len(sources) + len(dep_files)

    findings = [f for s in sources for f in s.findings]

    if "SLOP001" in rules:
        index = build_index(root, *project) if check_imports else build_index(root, [], dep_files)
        checker = PackageChecker(
            index, registry, known_packages=known_packages, private_index=private_index
        )
        checked_deps = {_rel_to(p, root) for p in dep_files}
        # Imports are only sent to PyPI when the user opts in (they can reveal private
        # names), and never for files outside the diff.
        in_scope = [s for s in sources if check_imports and (changed is None or s.rel in changed)]
        # One batch with every name the two checks below will ask about.
        checker.prefetch(checked_deps, (name for s in in_scope for name, _, _ in s.imports))
        findings.extend(checker.check_dependencies(only_paths=checked_deps))
        if private_index:
            result.notes.append(
                f"SLOP001 made no PyPI lookups: a private package index is configured in "
                f"{private_index}, so packages missing from PyPI may be private. If that "
                "index only mirrors PyPI, set detect-private-index = false."
            )
        elif check_imports and checker.imports_may_be_private:
            files = ", ".join(index.private_index_files)
            result.notes.append(
                f"SLOP001 didn't look up imports on PyPI: {files} uses a private package "
                "index, so unknown imports may be private packages."
            )
        for summary in in_scope:
            findings.extend(checker.check_imports(summary.rel, summary.imports))

    if "SLOP030" in rules:
        # Checked files first, then the rest of the project: the order decides which
        # copy a duplicate points at.
        functions = [f for s in (*sources, *compared_with) for f in s.functions]
        findings.extend(check_duplicates(functions, {s.rel for s in sources}, changed))

    # The filters, always in this order: ignore comments, then --diff.
    by_rel = {s.rel: s for s in sources}
    findings = [
        f for f in findings if not (f.path in by_rel and by_rel[f.path].is_ignored(f.rule, f.line))
    ]
    if changed is not None:
        findings = [f for f in findings if diffmod.touches(changed, f.path, f.line, f.end_line)]

    # The message breaks ties: ``import a, b`` gives two findings at one position, and
    # without it their order would be the set's, which changes with the hash seed.
    result.findings = sorted(
        set(findings), key=lambda f: (f.path, f.line, f.col, f.rule, f.message)
    )
    return result
