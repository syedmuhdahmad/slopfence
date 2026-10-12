"""File discovery and running detectors over a project."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from slopfence import baseline as baselinemod
from slopfence import diff as diffmod
from slopfence.analysis import FileSummary, Options, Task, analyze_files
from slopfence.config import ignored_rules, is_excluded, selected_rules
from slopfence.detectors import FILE_DETECTORS
from slopfence.detectors.dependency import NOTEBOOK_SUFFIX, kind_of
from slopfence.detectors.duplicates import check_duplicates
from slopfence.detectors.packages import (
    LOCK_FILES,
    PACKAGE_RULES,
    PackageChecker,
    build_index,
)
from slopfence.models import RULES, Finding, Severity
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
    # Jupyter's autosaved copies of notebooks.
    ".ipynb_checkpoints",
}


def _is_checkout(folder: Path) -> bool:
    """Whether a folder holds a ``.git`` file or folder: a clone, worktree or submodule.

    Asked with ``os.path.exists``, which has no answer but yes or no. Before Python
    3.13 ``Path.exists`` raises for a folder that can't be entered, and a walk must
    pass by such a folder, not end there.
    """
    return os.path.exists(folder / ".git")


def _is_pruned(folder: Path) -> bool:
    """Whether the walk of a project never enters ``folder``.

    That is so for an excluded tree (``.venv``, ``node_modules``, ``.git``...) and for
    a nested checkout. A sub-folder that holds a ``.git`` file or folder is a
    worktree, clone or submodule: another copy of a project, not this one. Its files
    must not count as this project's code (SLOP030 would double-report; SLOP001
    would treat its modules as local and its dependency files as this project's,
    see #58).
    """
    name = folder.name
    return name in EXCLUDED_DIRS or name.endswith(".egg-info") or _is_checkout(folder)


def discover_all(
    paths: Sequence[Path], root: Path, deep: bool = True
) -> tuple[list[Path], list[Path], list[Path]]:
    """Return (python files, dependency files, lock files) under the given paths.

    ``setup.py`` is both a Python file and a dependency file. ``root`` is the project
    root: whether a file is a dependency file can depend on its folder inside the
    project (``requirements/base.txt``, see ``dependency.kind_of``). With ``deep``
    False only the files that lie directly in a folder are taken, and none of the
    folders inside it is entered.
    """
    py_files: list[Path] = []
    dep_files: list[Path] = []
    lock_files: list[Path] = []

    def add(path: Path) -> None:
        """Put one file on the lists it belongs to."""
        if path.suffix == ".py":
            py_files.append(path)
        if kind_of(path, root) is not None:
            dep_files.append(path)
        elif path.name in LOCK_FILES:
            lock_files.append(path)

    for path in paths:
        path = path.resolve()
        if path.is_file():
            add(path)
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            # Pruned before descending. The path passed on the command line is the
            # walk root and is never pruned, so an explicitly named checkout is still
            # checked.
            dirnames[:] = [d for d in dirnames if deep and not _is_pruned(Path(dirpath, d))]
            for filename in filenames:
                add(Path(dirpath, filename))
    return sorted(set(py_files)), sorted(set(dep_files)), sorted(set(lock_files))


def _home(path: Path, root: Path) -> tuple[Path, bool] | None:
    """The folder a named path belongs to when the walk of the project never reaches
    it, and whether that folder is a checkout.

    None for a path that the walk reaches: it belongs to the project. The walk never
    leaves ``root`` and never enters a pruned folder (see ``_is_pruned``). A file or
    folder that is named there is checked all the same. It belongs to its own
    checkout: the nearest folder above it that holds a ``.git``. A file without one
    belongs to its own folder. A folder without one is on its own, like a folder that
    is a checkout itself, or the root, or a folder above the root: the walk of the
    named folder reads all there is.
    """
    is_folder = path.is_dir()
    if is_folder and (path == root or path in root.parents or _is_checkout(path)):
        return None
    above = list(path.parents)
    if root in above:
        above = above[: above.index(root)]
        if not any(_is_pruned(folder) for folder in above):
            return None
    checkout = next((folder for folder in above if _is_checkout(folder)), None)
    if checkout is not None:
        return checkout, True
    return None if is_folder else (path.parent, False)


def _beside_named_paths(paths: Sequence[Path], root: Path) -> tuple[list[Path], list[Path]]:
    """The dependency and lock files that named paths outside the project walk are
    judged with: those of their own checkout or folder (see ``_home``).

    Such a path is read like one of the project. Without its neighbours, a private
    index that its own checkout sets would not cover it, and its names would be sent.
    A checkout is walked like a project. Of a folder that is no checkout, only the
    files that lie directly in it count: what lies below a folder that happens to
    hold a named file (a home folder, a shared drive) is nobody's project.
    """
    homes = {home for path in paths if (home := _home(Path(path).resolve(), root))}
    deps: list[Path] = []
    locks: list[Path] = []
    for deep in (True, False):
        # The usual case has no home at all: every named path is one the walk reaches.
        if folders := sorted(folder for folder, is_checkout in homes if is_checkout is deep):
            _, found_deps, found_locks = discover_all(folders, root, deep)
            deps += found_deps
            locks += found_locks
    return deps, locks


def _not_excluded(files: list[Path], root: Path, exclude: Sequence[str]) -> list[Path]:
    """``files`` without the ones that match an ``exclude`` pattern."""
    if not exclude:
        return files  # the usual case, and no relative path has to be worked out
    return [p for p in files if not is_excluded(_rel_to(p, root), exclude)]


def _dependency_files_to_read(
    project: list[Path], checked: list[Path], root: Path, exclude: Sequence[str]
) -> set[Path]:
    """The dependency files that SLOP001 reads: the checked ones, and the rest of the project.

    Every file of the project is read, wherever it is and whichever paths the run
    checks: a private index, the name of one of the project's own packages, or a
    package that one file takes from git or a URL counts for the whole project. An
    excluded notebook is the one exception. A notebook is the biggest kind of file
    to read, and ``exclude`` is how a project leaves its notebooks out.

    In no order: ``packages.build_index`` puts the files in the one order of the run.
    """
    notebooks = _not_excluded(project, root, exclude)
    return {
        *checked,
        *(p for p in project if p.suffix != NOTEBOOK_SUFFIX),
        *(p for p in notebooks if p.suffix == NOTEBOOK_SUFFIX),
    }


@dataclass
class Result:
    findings: list[Finding] = field(default_factory=list)
    files_checked: int = 0
    parse_errors: list[str] = field(default_factory=list)
    # Things the user should know about the run, e.g. why lookups were skipped.
    notes: list[str] = field(default_factory=list)
    # The severities the config sets (rule ID -> severity), for reports that list rules.
    severities: dict[str, Severity] = field(default_factory=dict)
    # The findings the baseline hid. With --diff, only the ones on changed lines.
    baselined: list[Finding] = field(default_factory=list)
    # The package rules (SLOP001...) that ran without all their answers from PyPI: the
    # run was offline, the environment has a private index, or PyPI did not answer.
    # Their findings may be incomplete.
    unchecked_rules: list[str] = field(default_factory=list)


def _per_file_matcher(
    per_file_ignores: Mapping[str, Iterable[str]],
) -> Callable[[str, str], bool]:
    """A function that says whether ``per-file-ignores`` switches a rule off for a file.

    It takes a rule ID and a project-relative POSIX path. The patterns are matched
    once per file, however many findings the file has.
    """
    off: dict[str, frozenset[str]] = {}

    def switched_off(rule: str, rel: str) -> bool:
        """Whether ``rule`` is switched off for the file ``rel``."""
        if rel not in off:
            off[rel] = ignored_rules(rel, per_file_ignores)
        return rule in off[rel]

    return switched_off


def _with_severities(findings: list[Finding], severity: Mapping[str, Severity]) -> list[Finding]:
    """``findings``, each with the severity the config sets for its rule.

    A finding whose rule keeps its own severity is not touched, so it stays equal to
    the finding of a run without the config.
    """
    return [
        replace(f, severity_override=level)
        if (level := severity.get(f.rule)) not in (None, RULES[f.rule].severity)
        else f
        for f in findings
    ]


def _on_changed_lines(findings: list[Finding], changed: diffmod.ChangedLines) -> list[Finding]:
    """The findings that ``--diff`` reports: the ones that touch a changed line."""
    return [f for f in findings if diffmod.touches(changed, f.path, f.line, f.end_line)]


def _finish(
    result: Result,
    findings: Iterable[Finding],
    sources: Iterable[FileSummary],
    switched_off: Callable[[str, str], bool],
    *,
    severity: Mapping[str, Severity],
    baseline: baselinemod.Baseline | None,
    changed: diffmod.ChangedLines | None,
    rules: set[str],
    scope: baselinemod.Scope,
    root: Path,
) -> None:
    """Filter, order and label the findings of every rule, and put them in ``result``.

    The filters, always in this order: ignore comments, per-file ignores, severities
    from the config (they only change a label), the baseline, then ``--diff``. A new
    rule needs nothing here: it only hands in its findings.

    ``sources`` are the files that were checked and ``switched_off`` says where
    ``per-file-ignores`` switch a rule off. ``rules`` are the rules that ran and
    ``scope`` is what the run set out to check (see ``baseline.scope_of``): the two
    say which baselined findings the run should have found again.
    """
    by_rel = {s.rel: s for s in sources}
    findings = [
        f
        for f in findings
        if not (f.path in by_rel and by_rel[f.path].is_ignored(f.rule, f.line))
        and not switched_off(f.rule, f.path)
    ]
    # The one sort. The message breaks ties: ``import a, b`` gives two findings at one
    # position, and without it their order would be the set's, which changes with the
    # hash seed.
    findings = sorted(set(findings), key=lambda f: (f.path, f.line, f.col, f.rule, f.message))
    if severity:
        findings = _with_severities(findings, severity)
    if baseline is not None:
        # Before --diff: an old finding outside the diff must use up its entry, or a
        # new one like it on a changed line would be hidden in its place.
        matched = baselinemod.match(findings, baseline, changed)
        findings, result.baselined = matched.new, matched.hidden
        # With --diff some rules only look at changed code, so an entry that nothing
        # used says nothing. Nor does one of a package rule: PyPI may not have answered.
        if changed is None:
            note = baselinemod.stale_note(
                baseline, matched.unused, rules - PACKAGE_RULES, by_rel.keys(), scope, root
            )
            if note:
                result.notes.append(note)
    if changed is not None:
        findings = _on_changed_lines(findings, changed)
        result.baselined = _on_changed_lines(result.baselined, changed)
    result.findings = findings


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
    per_file_ignores: Mapping[str, Iterable[str]] | None = None,
    severity: Mapping[str, Severity] | None = None,
    baseline: baselinemod.Baseline | None = None,
    inherit_index: bool = True,
) -> Result:
    """Check ``paths`` and return the findings.

    SLOP001 always checks dependency files. Imports in source code are only looked
    up on PyPI with ``check_imports``, because that sends their names to pypi.org
    and can reveal private package names.

    ``private_index`` says where the environment configures a non-PyPI package
    index (see ``indexes.find_private_index``); SLOP001 then makes no lookups.

    A private index in a project file covers that file. With ``inherit_index`` it also
    covers the dependency files that name no index of their own (``setup.py``,
    ``setup.cfg``, ``Pipfile``, notebooks, and requirements files that v0.2 did not
    read), unless the file that names it is excluded.

    Files are parsed and checked in up to ``jobs`` worker processes when there are
    enough of them (see ``analysis.analyze_files``). The result does not depend on
    ``jobs``. Registry lookups always happen in this process.

    ``per_file_ignores`` maps patterns (the kind ``exclude`` takes) to the rules that
    are switched off in the matching files. ``severity`` gives rules another severity.
    ``baseline`` holds findings that were accepted earlier: they are left out of
    ``Result.findings`` and listed in ``Result.baselined``.
    """
    rules = selected_rules(select, ignore)
    root = root.resolve()

    found_py, found_deps, found_locks = discover_all(paths, root)
    # Excluded files are not checked, but still count as project modules below.
    py_files = _not_excluded(found_py, root, exclude)
    dep_files = _not_excluded(found_deps, root, exclude)

    # SLOP030 and SLOP001 look at the whole project, not just the checked paths. When
    # pre-commit passes only changed files, their functions and imports are still
    # compared with the rest. And a private index, or the name of one of the project's
    # own packages, counts wherever in the project it is written.
    project = (found_py, found_deps, found_locks)
    whole_project_given = any(Path(p).resolve() == root for p in paths)
    if rules & {"SLOP001", "SLOP030"} and not whole_project_given:
        project = discover_all([root], root)

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

    result = Result(severities=dict(severity or {}))
    switched_off = _per_file_matcher(per_file_ignores or {})
    result.parse_errors = [s.rel for s in checked if not s.parsed]
    sources = [s for s in checked if s.parsed]
    # setup.py is a Python file and a dependency file: it is counted once.
    result.files_checked = len(sources) + sum(p.suffix != ".py" for p in dep_files)

    findings = [f for s in sources for f in s.findings]

    if "SLOP001" in rules:
        beside_deps, beside_locks = _beside_named_paths(paths, root)
        to_read = _dependency_files_to_read([*project[1], *beside_deps], dep_files, root, exclude)
        lock_files = [*project[2], *beside_locks]
        index = build_index(root, project[0], to_read, lock_files, imports=check_imports)
        checker = PackageChecker(
            index,
            registry,
            known_packages=known_packages,
            private_index=private_index,
            # A private index in an excluded file is the user's to leave out.
            inherited_from=[
                rel
                for rel in index.install_index_files
                if inherit_index and not is_excluded(rel, exclude)
            ],
        )
        # The names of these files, and of no other file, may go to PyPI. A file where
        # per-file-ignores switch SLOP001 off is left out here, where the two collections
        # are made: the batch below and both checks are fed from them. So is a file
        # outside the diff: its findings would be dropped.
        checked_deps = {
            rel
            for p in dep_files
            if not switched_off("SLOP001", rel := _rel_to(p, root))
            and (changed is None or rel in changed)
        }
        # Imports are only sent to PyPI when the user opts in (they can reveal private
        # names), and never for files outside the diff. Nor is an import under an ignore
        # comment: its finding would be dropped, so there is nothing to ask for.
        imports = {
            s.rel: [found for found in s.imports if not s.is_ignored("SLOP001", found[1])]
            for s in sources
            if check_imports
            and (changed is None or s.rel in changed)
            and not switched_off("SLOP001", s.rel)
        }
        # The imports that checked dependency files hold themselves, under the same opt-in.
        imports.update(checker.imports_in_files(checked_deps))
        imported = [name for found in imports.values() for name, _, _ in found]
        # One batch with every name the two checks below will ask about.
        checker.prefetch(checked_deps, imported)
        findings.extend(checker.check_dependencies(only_paths=checked_deps))
        result.notes.extend(checker.notes(checked_deps, check_imports))
        for rel, found in imports.items():
            findings.extend(checker.check_imports(rel, found))
        if checker.lookups_unanswered(checked_deps, imported):
            result.unchecked_rules = sorted(rules & PACKAGE_RULES)

    if "SLOP030" in rules:
        project_files = (*sources, *compared_with)
        findings.extend(
            check_duplicates(
                [f for s in project_files for f in s.functions],
                {s.rel for s in sources},
                changed,
                # A copy of code from a file where SLOP030 is off is still reported.
                ignored={s.rel for s in project_files if switched_off("SLOP030", s.rel)},
                # With --diff, a baseline must also see what did not change.
                report_unchanged=baseline is not None,
            )
        )

    _finish(
        result,
        findings,
        sources,
        switched_off,
        severity=severity or {},
        baseline=baseline,
        changed=changed,
        rules=rules,
        scope=baselinemod.scope_of(paths, root, exclude, rules),
        root=root,
    )
    return result
