"""One file at a time: what ``analyze_file`` keeps of a file, and what the engine does with it."""

from slopfence import engine
from slopfence.analysis import FileSummary, Options, analyze_file, analyze_files
from slopfence.detectors import FILE_DETECTORS
from slopfence.engine import run
from slopfence.registry import OfflineRegistry
from slopfence.source import ALL_RULES, load
from tests.conftest import FakeRegistry

HELPER = '''
def format_date(value, fmt="%Y-%m-%d"):
    """Format a date for display."""
    if value is None:
        return ""
    text = value.strftime(fmt)
    return text.strip()
'''


def options_for(root, rules=frozenset(FILE_DETECTORS)):
    """Options that run ``rules`` and collect everything the project-wide rules need."""
    return Options(str(root), frozenset(rules), functions=True, imports=True)


def test_summary_of_a_checked_file(write, tmp_path):
    """A summary carries findings, imports, fingerprints and the file's ignore comments."""
    path = write(
        "src/app.py",
        "import os, ghost_a\nfrom ghost_b.sub import x  # slopfence: ignore[SLOP001]\n"
        "from . import sibling\n# I hope this helps!\n" + HELPER,
    )
    summary = analyze_file(options_for(tmp_path, {"SLOP051"}), (str(path), True))
    assert (summary.rel, summary.parsed) == ("src/app.py", True)
    assert [(f.rule, f.line) for f in summary.findings] == [("SLOP051", 4)]
    # Absolute imports only, one entry per name, in the order they are written.
    assert summary.imports == [("os", 1, 1), ("ghost_a", 1, 1), ("ghost_b", 2, 1)]
    assert [(f.name, f.path, f.line) for f in summary.functions] == [
        ("format_date", "src/app.py", 6)
    ]
    assert summary.is_ignored("SLOP001", 2) and not summary.is_ignored("SLOP030", 2)
    assert not summary.is_ignored("SLOP001", 1)


def test_summary_keeps_findings_that_an_ignore_comment_covers(write, tmp_path):
    """The engine applies ignore comments, in one place; a summary only carries them."""
    path = write("app.py", "# I hope this helps!  # slopfence: ignore\n# slopfence: ignore-file\n")
    summary = analyze_file(options_for(tmp_path), (str(path), True))
    assert [f.rule for f in summary.findings] == ["SLOP051"]
    assert summary.ignore_file and summary.is_ignored("SLOP051", 1)
    assert run([tmp_path], tmp_path, OfflineRegistry()).findings == []


def test_summary_only_holds_what_was_asked_for(write, tmp_path):
    """Rules that are off don't run, and facts no rule needs aren't collected."""
    path = write("app.py", "import ghost_a\n# I hope this helps!\n" + HELPER)
    nothing = Options(str(tmp_path), frozenset())
    assert analyze_file(nothing, (str(path), True)) == FileSummary(rel="app.py")


def test_file_that_cannot_be_parsed(write, tmp_path):
    """A broken or missing file gives a summary that says so, and nothing else."""
    broken = write("src/broken.py", "def broken(:\n")
    options = options_for(tmp_path)
    assert analyze_file(options, (str(broken), True)) == FileSummary("src/broken.py", parsed=False)
    gone = tmp_path / "src" / "gone.py"
    assert analyze_file(options, (str(gone), False)) == FileSummary("src/gone.py", parsed=False)


def test_file_that_is_only_compared_with(write, tmp_path):
    """Unchecked files give fingerprints and nothing else; test code isn't even read."""
    options = options_for(tmp_path)
    sloppy = write("src/sloppy.py", "import ghost_a\n# I hope this helps!\n" + HELPER)
    summary = analyze_file(options, (str(sloppy), False))
    assert [f.name for f in summary.functions] == ["format_date"]
    assert (summary.findings, summary.imports) == ([], [])
    # Not a file at all: reading it would fail, so the summary shows it wasn't read.
    test_dir = tmp_path / "tests" / "test_helpers.py"
    test_dir.mkdir(parents=True)
    assert analyze_file(options, (str(test_dir), False)) == FileSummary("tests/test_helpers.py")


def test_comments_are_not_read_for_a_file_that_is_only_compared_with(write, tmp_path):
    """``load(comments=False)`` skips the comments, and so the ignore directives."""
    path = write("app.py", "x = 1  # slopfence: ignore\n# slopfence: ignore-file\n")
    src = load(path, tmp_path, comments=False)
    assert (src.comments, src.ignores, src.ignore_file) == ([], {}, False)
    src = load(path, tmp_path)
    assert (len(src.comments), src.ignores, src.ignore_file) == (2, {1: ALL_RULES}, True)


def test_analyze_files_keeps_the_order_of_the_tasks(write, tmp_path):
    """One summary per task, in task order: the engine relies on it to tell files apart."""
    names = ["b.py", "a.py", "c.py"]
    tasks = [(str(write(name, "x = 1\n")), True) for name in names]
    assert [s.rel for s in analyze_files(tasks, options_for(tmp_path))] == names


def test_one_checked_file_is_compared_with_the_rest_of_the_project(write, tmp_path):
    """Pre-commit passes one file: its copy elsewhere is found, and only it is checked."""
    write("src/helpers.py", HELPER)
    write("tests/test_helpers.py", HELPER)  # test code never counts as a copy
    write("src/sloppy.py", "# In a real implementation, retry\n")
    write("generated/copy.py", HELPER)  # excluded: not checked and not compared with
    target = write("src/views.py", HELPER.replace("format_date", "fmt_date"))
    result = run([target], tmp_path, OfflineRegistry(), exclude=["generated"])
    assert [(f.rule, f.path) for f in result.findings] == [("SLOP030", "src/views.py")]
    assert "'format_date' in src/helpers.py:1" in result.findings[0].message
    assert result.files_checked == 1


def test_ignore_comments_cover_every_kind_of_finding(write, tmp_path):
    """Per-file rules, SLOP001 imports and SLOP030 all obey a comment on their line."""
    write("src/helpers.py", HELPER)
    body = HELPER.replace("format_date", "fmt_date").lstrip("\n")
    lines = ["import ghost_pkg_zz", "# I hope this helps!", *body.splitlines()]
    path = write("src/views.py", "\n".join(lines) + "\n")
    registry = FakeRegistry(set())

    def rules():
        """The rules reported for views.py, in order."""
        result = run([tmp_path], tmp_path, registry, check_imports=True)
        return [(f.rule, f.line) for f in result.findings if f.path == "src/views.py"]

    assert rules() == [("SLOP001", 1), ("SLOP051", 2), ("SLOP030", 3)]
    lines[0] += "  # slopfence: ignore[SLOP001]"
    lines[2] += "  # slopfence: ignore[SLOP030]"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert rules() == [("SLOP051", 2)]
    # A comment that names another rule changes nothing.
    lines[1] += "  # slopfence: ignore[SLOP010]"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert rules() == [("SLOP051", 2)]


def test_project_is_listed_once_and_broken_files_are_read_once(write, tmp_path, monkeypatch):
    """One checked file, two project-wide rules: one walk of the project, and a file
    that can't be parsed isn't tried again as part of "the rest of the project"."""
    write("src/helpers.py", HELPER)
    broken = write("src/broken.py", "def broken(:\n")
    walks, batches = [], []
    real_discover, real_analyze = engine.discover_all, engine.analyze_files
    monkeypatch.setattr(
        engine,
        "discover_all",
        lambda paths, root: walks.append(list(paths)) or real_discover(paths, root),
    )
    monkeypatch.setattr(
        engine, "analyze_files", lambda tasks, *a: batches.append(tasks) or real_analyze(tasks, *a)
    )
    result = run([broken], tmp_path, FakeRegistry(set()), check_imports=True)
    assert result.parse_errors == ["src/broken.py"] and result.files_checked == 0
    root = tmp_path.resolve()
    assert walks == [[broken], [root]]
    [tasks] = batches
    assert tasks == [(str(broken.resolve()), True), (str(root / "src" / "helpers.py"), False)]
