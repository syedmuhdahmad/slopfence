"""File discovery and running detectors over a project."""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from slopfence import diff as diffmod
from slopfence.config import is_excluded
from slopfence.detectors import FILE_DETECTORS
from slopfence.detectors.duplicates import check_duplicates
from slopfence.detectors.packages import LOCK_FILES, PackageChecker, build_index
from slopfence.models import RULES, Finding
from slopfence.registry import Registry
from slopfence.source import SourceFile, load
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


def discover(paths: Sequence[Path]) -> tuple[list[Path], list[Path]]:
    """Return (python files, dependency files) under the given paths."""
    py_files, dep_files, _ = discover_all(paths)
    return py_files, dep_files


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
) -> Result:
    """Check ``paths`` and return the findings.

    SLOP001 always checks dependency files. Imports in source code are only looked
    up on PyPI with ``check_imports``, because that sends their names to pypi.org
    and can reveal private package names.

    ``private_index`` says where the environment configures a non-PyPI package
    index (see ``indexes.find_private_index``); SLOP001 then makes no lookups.
    """
    rules = set(select) if select else set(RULES)
    rules -= set(ignore)
    root = root.resolve()

    found_py, found_deps, found_locks = discover_all(paths)
    # Excluded files are not checked, but still count as project modules below.
    py_files = [p for p in found_py if not is_excluded(_rel_to(p, root), exclude)]
    dep_files = [p for p in found_deps if not is_excluded(_rel_to(p, root), exclude)]
    result = Result()
    sources: list[SourceFile] = []
    for path in py_files:
        src = load(path, root)
        if src is None:
            result.parse_errors.append(_rel_to(path, root))
            continue
        sources.append(src)
    result.files_checked = len(sources) + len(dep_files)

    findings: list[Finding] = []
    for src in sources:
        for rule, detector in FILE_DETECTORS.items():
            if rule in rules:
                findings.extend(f for f in detector(src) if not src.is_ignored(f.rule, f.line))

    if "SLOP001" in rules:
        if not check_imports:
            index = build_index(root, [], dep_files)
        # Index the whole project, not just the checked paths: when pre-commit passes
        # only changed files, imports of the project's other modules must still resolve.
        elif any(Path(p).resolve() == root for p in paths):
            index = build_index(root, found_py, found_deps, found_locks)
        else:
            index = build_index(root, *discover_all([root]))
        checker = PackageChecker(
            index, registry, known_packages=known_packages, private_index=private_index
        )
        checked = {_rel_to(p, root) for p in dep_files}
        findings.extend(checker.check_dependencies(only_paths=checked))
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
        # Imports are only sent to PyPI when the user opts in (they can reveal private names).
        for src in sources if check_imports else ():
            if changed is not None and src.rel not in changed:
                continue  # skip network lookups for files outside the diff
            findings.extend(
                f for f in checker.check_imports(src) if not src.is_ignored(f.rule, f.line)
            )

    if "SLOP030" in rules:
        # Compare against every function in the project, not just the checked files.
        if any(Path(p).resolve() == root for p in paths):
            project = sources
        else:
            checked_paths = {s.path.resolve() for s in sources}
            project = list(sources)
            for path in discover([root])[0]:
                if path.resolve() in checked_paths or is_excluded(_rel_to(path, root), exclude):
                    continue
                if (src := load(path, root)) is not None:
                    project.append(src)
        by_rel = {s.rel: s for s in sources}
        findings.extend(
            f
            for f in check_duplicates(project, set(by_rel), changed)
            if not by_rel[f.path].is_ignored(f.rule, f.line)
        )

    if changed is not None:
        findings = [f for f in findings if diffmod.touches(changed, f.path, f.line, f.end_line)]

    result.findings = sorted(set(findings), key=lambda f: (f.path, f.line, f.col, f.rule))
    return result
