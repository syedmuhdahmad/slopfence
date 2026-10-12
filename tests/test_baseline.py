"""Baseline mode (#51): accept today's findings, report only new ones."""

import ast
import difflib
import json
import os
import random
import subprocess
from pathlib import Path

import pytest

import slopfence
from slopfence import baseline as baselinemod
from slopfence import cli
from slopfence.baseline import Baseline, BaselineError, key_of, normalize_fingerprint
from slopfence.cli import main
from slopfence.config import load_config
from slopfence.engine import run
from slopfence.models import RULES, Finding
from slopfence.registry import OfflineRegistry, PyPIRegistry
from tests.conftest import FakeRegistry

APP = '''\
import os


def charge(card):
    # In a real implementation, call the payment provider
    return True


def validate_token(token):
    """Validate the token signature."""
    return True


def load(path):
    try:
        return open(path).read()
    except Exception:
        pass
# I hope this helps!
'''
HELPER = (
    "def {name}(value, fmt='%Y'):\n    if value is None:\n        return ''\n"
    "    text = value.strftime(fmt)\n    return text.strip()\n"
)
TESTS = """\
from unittest.mock import Mock


def test_user():
    user = Mock()
    user.name = "x"
    assert user.name == "x"


def test_nothing():
    assert True


def test_never_fails():
    try:
        assert compute() == 1
    except Exception:
        pass
"""
# What the finding of each rule in the ``project`` fixture is about. A new rule needs a
# finding in the fixture and its subject here: test_every_rule_names_a_subject fails
# until it has both.
SUBJECTS = {
    "SLOP001": "flask-jwt-simple-auth",
    "SLOP010": "In a real implementation, call the payment provider",
    "SLOP011": "validate_token",
    "SLOP020": "test_user",
    "SLOP021": "test_nothing",
    "SLOP022": "test_never_fails",
    "SLOP030": "fmt_date",
    "SLOP040": "load",
    "SLOP051": "I hope this helps!",
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project with one finding of every rule; PyPI answers come from a pinned cache."""
    files = {
        "src/app.py": APP,
        "src/dates.py": HELPER.format(name="format_date"),
        "src/utils.py": HELPER.format(name="fmt_date"),
        "tests/test_app.py": TESTS,
        "requirements.txt": "requests>=2\nflask-jwt-simple-auth==1.0\n",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    cache = tmp_path / "cache" / "slopfence"
    cache.mkdir(parents=True)
    (cache / "pypi.json").write_text(
        json.dumps({"requests": [True, 9e12], "flask-jwt-simple-auth": [False, 9e12]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SLOPFENCE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def report(capsys, *args):
    """Run slopfence on the current folder with JSON output: (exit code, report)."""
    capsys.readouterr()
    code = main([".", "--format", "json", *args])
    return code, json.loads(capsys.readouterr().out)


def rows(path):
    """The rows of a baseline file."""
    return json.loads(path.read_text(encoding="utf-8"))["findings"]


def prepend(path, text):
    """Add ``text`` at the top of a file."""
    path.write_text(text + path.read_text(encoding="utf-8"), encoding="utf-8")


def edit(path, old, new):
    """Replace ``old`` with ``new`` in a file. The file must hold ``old``."""
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def git(cwd, *args):
    """Run a git command in ``cwd``."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def commit_all(cwd):
    """Make ``cwd`` a git repository with everything in it committed on main."""
    git(cwd, "init", "-q", "-b", "main")
    git(cwd, "config", "user.email", "t@example.com")
    git(cwd, "config", "user.name", "t")
    (cwd / ".gitignore").write_text("cache/\n", encoding="utf-8")
    git(cwd, "add", ".")
    git(cwd, "commit", "-q", "-m", "init")


GONE = "{} in the baseline no longer {}. Run --write-baseline again to update the file."


# --- what a finding is about ----------------------------------------------------------


def test_every_rule_names_a_subject(project):
    """Each rule says what its finding is about, so a baseline survives edits."""
    result = run([project], project, FakeRegistry({"requests"}))
    assert {f.rule: f.subject for f in result.findings} == SUBJECTS
    assert len(result.findings) == len(SUBJECTS)
    # Every rule in RULES, not a hand-written list: a new rule without a finding in
    # the fixture, or without a subject, fails here.
    assert set(SUBJECTS) == set(RULES)


def test_every_finding_in_the_package_is_built_with_a_subject():
    """No detector can forget the subject on one of its paths: each ``Finding(...)``
    call in the package passes ``subject=``."""
    missing = []
    for path in sorted(Path(slopfence.__file__).parent.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Finding"
                and not any(keyword.arg == "subject" for keyword in node.keywords)
            ):
                missing.append(f"{path.name}:{node.lineno}")
    assert missing == []


def test_import_findings_are_about_the_imported_module(write, tmp_path):
    """SLOP001 for an import names the module, wherever and however it is imported."""
    write("app.py", "import made_up_zz\nfrom made_up_zz.core import x\n")
    result = run([tmp_path], tmp_path, FakeRegistry(set()), select=["SLOP001"], check_imports=True)
    assert [(f.line, f.subject) for f in result.findings] == [(1, "made_up_zz"), (2, "made_up_zz")]


def test_dependency_findings_are_about_the_normalized_name(write, check):
    """``Flask_JWT.Simple`` and ``flask-jwt-simple`` are the same package on PyPI."""
    write("requirements.txt", "Made_Up.Package_zz==1.0\n")
    result = check("SLOP001", registry=FakeRegistry(set()))
    assert [f.subject for f in result.findings] == ["made-up-package-zz"]


def test_swallowed_exception_outside_a_function_has_a_fixed_subject(write, check):
    """SLOP040 at module level is about "<module>": there is no function to name."""
    write("app.py", "try:\n    import_plugins()\nexcept Exception:\n    pass\n")
    assert [f.subject for f in check("SLOP040").findings] == ["<module>"]


def test_comment_findings_are_about_the_whole_comment(write, check):
    """The message cuts a long comment at 80 characters; the subject keeps all of it."""
    comment = "In a real implementation, " + "retry with a longer and longer delay " * 3
    write("app.py", f"#   {comment}\n")
    [finding] = check("SLOP010").findings
    assert finding.subject == comment.strip()
    assert finding.message.endswith('..."') and len(finding.message) < len(comment)


# --- writing a baseline and using it ----------------------------------------------------


def test_write_baseline_then_only_new_findings_fail(project, capsys):
    """--write-baseline accepts every finding; --baseline then reports only new ones."""
    code, before = report(capsys)
    assert code == 1
    assert {f["rule"] for f in before["findings"]} == set(RULES)
    assert before["baselined"] == 0  # the key is there without a baseline, too
    total = len(before["findings"])

    assert main([".", "--no-color", "--write-baseline", "base.json"]) == 0
    assert capsys.readouterr().out == (
        f"No new issues found in 5 files; {total} hidden by the baseline.\n"
        f"note: Wrote {total} findings to the baseline base.json.\n"
    )

    code, after = report(capsys, "--baseline", "base.json")
    assert (code, after["findings"], after["baselined"], after["notes"]) == (0, [], total, [])

    # A new finding is the only one reported, and the only one that counts.
    (project / "src" / "new.py").write_text("# Simplified implementation for demo\n")
    code, after = report(capsys, "--baseline", "base.json")
    assert code == 1
    assert [(f["path"], f["rule"]) for f in after["findings"]] == [("src/new.py", "SLOP010")]
    assert after["baselined"] == total
    assert main([".", "--no-color", "--baseline", "base.json"]) == 1
    out = capsys.readouterr().out
    assert out.strip().endswith(f"1 issue (1 medium); {total} more hidden by the baseline")
    assert "src/app.py" not in out


def test_baselined_findings_stay_hidden_when_lines_are_added_above(project, capsys):
    """Moving code never brings a baselined finding back: no line number is recorded."""
    main([".", "--write-baseline", "base.json"])
    for rel in ["src/app.py", "src/dates.py", "src/utils.py", "tests/test_app.py"]:
        prepend(project / rel, "# a new comment\n\nNEW_CONSTANT = 1\n\n")
    prepend(project / "requirements.txt", "# pinned by ops\nidna\n")
    (project / "cache" / "slopfence" / "pypi.json").write_text(
        json.dumps(
            {"requests": [True, 9e12], "idna": [True, 9e12], "flask-jwt-simple-auth": [False, 9e12]}
        ),
        encoding="utf-8",
    )
    # Without the baseline every finding is there, four lines (two in requirements) down.
    code, moved = report(capsys)
    assert (code, len(moved["findings"])) == (1, len(RULES))
    code, after = report(capsys, "--baseline", "base.json")
    assert (code, after["findings"], after["baselined"]) == (0, [], len(RULES))
    assert after["notes"] == []  # and nothing looks fixed


def test_unrelated_edits_keep_findings_hidden(project, capsys):
    """Edits that don't change what a finding is about keep it hidden."""
    main([".", "--write-baseline", "base.json"])
    app = project / "src" / "app.py"
    # A new signature, a comment spaced another way, a changed body, a new version
    # pin, and the other copy of the duplicate moved down.
    edit(app, "def validate_token(token):", "def validate_token(token, *, strict=False):")
    edit(app, "    # In a real implementation, call", "    #In a real implementation,   call")
    edit(app, "return open(path).read()", "return open(path, encoding='utf-8').read()")
    (project / "requirements.txt").write_text("requests>=3\nflask-jwt-simple-auth>=2.0  # new\n")
    # The SLOP030 message names dates.py:1. It moves; the finding is still about fmt_date.
    prepend(project / "src" / "dates.py", "import datetime\n")
    code, after = report(capsys, "--baseline", "base.json")
    assert (code, after["findings"], after["baselined"]) == (0, [], len(RULES))


def test_changed_subject_brings_a_finding_back(project, capsys):
    """Renaming the function or rewording the comment makes it a new finding."""
    main([".", "--write-baseline", "base.json"])
    app = project / "src" / "app.py"
    edit(app, "validate_token", "check_token")
    edit(app, "call the payment provider", "call Stripe")
    code, after = report(capsys, "--baseline", "base.json")
    assert code == 1
    assert sorted(f["rule"] for f in after["findings"]) == ["SLOP010", "SLOP011"]
    # The two rows that match nothing any more are pointed out.
    assert after["notes"] == [GONE.format("2 findings", "exist")]


def test_renamed_file_brings_its_findings_back(project, capsys):
    """A row names its file, so the findings of a renamed file are new ones."""
    main([".", "--write-baseline", "base.json"])
    (project / "src" / "app.py").rename(project / "src" / "application.py")
    code, after = report(capsys, "--baseline", "base.json")
    assert code == 1
    assert {f["path"] for f in after["findings"]} == {"src/application.py"}
    assert sorted(f["rule"] for f in after["findings"]) == [
        "SLOP010",
        "SLOP011",
        "SLOP040",
        "SLOP051",
    ]
    assert after["notes"] == [GONE.format("4 findings", "exist")]


def test_identical_findings_are_counted(write, tmp_path, monkeypatch, capsys):
    """Findings with the same key are matched by count; one more is reported."""
    swallow = "    try:\n        work()\n    except Exception:\n        pass\n"
    write("app.py", "def sync():\n" + swallow * 2)
    monkeypatch.chdir(tmp_path)
    main([".", "--offline", "--write-baseline", "base.json"])
    assert rows(tmp_path / "base.json") == [["app.py", "SLOP040", "sync", 2]]
    code, after = report(capsys, "--offline", "--baseline", "base.json")
    assert (code, after["baselined"]) == (0, 2)
    write("app.py", "def sync():\n" + swallow * 3)
    code, after = report(capsys, "--offline", "--baseline", "base.json")
    # Without --diff nothing tells old from new: the last one in the file is reported.
    assert (code, [f["line"] for f in after["findings"]], after["baselined"]) == (1, [12], 2)
    # One fixed: part of the row's count is left over.
    write("app.py", "def sync():\n" + swallow)
    code, after = report(capsys, "--offline", "--baseline", "base.json")
    assert (code, after["baselined"]) == (0, 1)
    assert after["notes"] == [GONE.format("1 finding", "exists")]


# --- together with other options --------------------------------------------------------


def test_baseline_with_diff_reports_the_changed_copy(write, tmp_path, monkeypatch, capsys):
    """With --diff, a new finding on a changed line is reported, also above an old twin."""
    swallow = "    try:\n        work()\n    except Exception:\n        pass\n"
    write("app.py", "def sync():\n" + swallow)
    old = "def charge(card):\n    # In a real implementation, pay\n    return True\n"
    write("old.py", old)
    monkeypatch.chdir(tmp_path)
    main([".", "--offline", "--write-baseline", "base.json"])
    commit_all(tmp_path)
    git(tmp_path, "switch", "-q", "-c", "feature")
    # A second swallowed exception above the old one, and an edit in old.py's function.
    write("app.py", "def sync():\n" + swallow.replace("work", "new") + swallow)
    write("old.py", old.replace("return True", "return card"))

    code, after = report(capsys, "--offline", "--diff", "main", "--baseline", "base.json")
    assert (code, [(f["path"], f["line"]) for f in after["findings"]]) == (1, [("app.py", 4)])
    # "baselined" counts the hidden findings on changed lines. There are none: the
    # old handler did not change, and neither did the comment line in old.py.
    assert after["baselined"] == 0
    assert after["notes"] == []  # with --diff nothing is ever called gone
    # Without --diff the baseline can't tell which of the two is new: the last is reported.
    code, after = report(capsys, "--offline", "--baseline", "base.json")
    assert [(f["path"], f["line"]) for f in after["findings"]] == [("app.py", 8)]


def test_baseline_hides_old_findings_on_changed_lines(write, tmp_path, monkeypatch, capsys):
    """--diff alone reports an old finding in a function you touched; the baseline hides it."""
    test = """
        from unittest.mock import Mock


        def test_user():
            user = Mock()
            user.name = 'x'
            assert user.name == 'x'
        """
    write("test_user.py", test)
    monkeypatch.chdir(tmp_path)
    main([".", "--offline", "--write-baseline", "base.json"])
    commit_all(tmp_path)
    git(tmp_path, "switch", "-q", "-c", "feature")
    write("test_user.py", test.replace("'x'", "'y'"))
    code, after = report(capsys, "--offline", "--diff", "main")
    assert (code, [f["rule"] for f in after["findings"]]) == (1, ["SLOP020"])
    code, after = report(capsys, "--offline", "--diff", "main", "--baseline", "base.json")
    assert (code, after["findings"], after["baselined"]) == (0, [], 1)


def test_baseline_with_fail_on_select_and_severity(project, capsys):
    """Only the findings the baseline doesn't hide count for --fail-on."""
    main([".", "--select", "SLOP001,SLOP020,SLOP022", "--write-baseline", "high.json"])
    assert [row[1] for row in rows(project / "high.json")] == ["SLOP001", "SLOP020", "SLOP022"]
    # Every high finding is baselined; the rest are medium or low.
    assert main([".", "--baseline", "high.json", "--fail-on", "high"]) == 0
    assert main([".", "--baseline", "high.json", "--fail-on", "medium"]) == 1
    # Rules that don't run this time don't make their rows look fixed.
    code, after = report(capsys, "--baseline", "high.json", "--select", "SLOP010")
    assert (code, after["notes"], after["baselined"]) == (1, [], 0)
    # A severity from the config changes a label, never what the baseline matches.
    (project / "pyproject.toml").write_text('[tool.slopfence.severity]\nSLOP001 = "low"\n')
    code, after = report(capsys, "--baseline", "high.json", "--fail-on", "high")
    assert (code, after["baselined"]) == (0, 3)


def test_sarif_leaves_baselined_findings_out(project, capsys):
    """SARIF lists only the new findings and says how many the baseline hid."""
    main([".", "--write-baseline", "base.json"])
    (project / "src" / "new.py").write_text("# I hope this helps!\n")
    capsys.readouterr()
    assert main([".", "--format", "sarif", "--baseline", "base.json"]) == 1
    sarif = json.loads(capsys.readouterr().out)["runs"][0]
    assert [r["ruleId"] for r in sarif["results"]] == ["SLOP051"]
    assert "baselineState" not in sarif["results"][0] and "suppressions" not in sarif["results"][0]
    notes = [n["message"]["text"] for n in sarif["invocations"][0]["toolExecutionNotifications"]]
    assert notes == ["9 findings hidden by the baseline"]
    # Without a baseline the notification is not there.
    main([".", "--format", "sarif"])
    plain = json.loads(capsys.readouterr().out)["runs"][0]
    assert plain["invocations"][0]["toolExecutionNotifications"] == []


def test_per_file_ignored_findings_do_not_use_baseline_entries(project, capsys):
    """Per-file ignores run first; the row they make needless is reported as gone."""
    main([".", "--write-baseline", "base.json"])
    (project / "pyproject.toml").write_text(
        '[tool.slopfence.per-file-ignores]\n"src/app.py" = ["SLOP040"]\n'
    )
    code, after = report(capsys, "--baseline", "base.json")
    assert (code, after["baselined"]) == (0, 8)
    assert after["notes"] == [GONE.format("1 finding", "exists")]


def test_ignore_comment_on_a_baselined_finding_wins(project, capsys):
    """An ignore comment hides the finding before the baseline sees it."""
    main([".", "--write-baseline", "base.json"])
    edit(
        project / "src" / "app.py",
        "    except Exception:",
        "    except Exception:  # slopfence: ignore",
    )
    code, after = report(capsys, "--baseline", "base.json")
    assert (code, after["baselined"]) == (0, 8)
    assert after["notes"] == [GONE.format("1 finding", "exists")]


# --- where the file is looked for -------------------------------------------------------


def test_baseline_config_key_is_relative_to_the_project_root(project, capsys, monkeypatch):
    """The config key is resolved from the project root, --baseline from the current folder."""
    (project / "ci").mkdir()
    main([".", "--write-baseline", "ci/base.json"])
    (project / "pyproject.toml").write_text('[tool.slopfence]\nbaseline = "ci/base.json"\n')
    commit_all(project)  # a git repository: the root is the same from every folder in it
    assert report(capsys)[0] == 0

    monkeypatch.chdir(project / "src")
    assert main([".", "--format", "json"]) == 0
    assert main([".", "--baseline", "../ci/base.json"]) == 0  # relative to this folder
    capsys.readouterr()
    assert main([".", "--baseline", "ci/base.json"]) == 2  # not relative to the root
    # Paths are printed the way the platform writes them (backslashes on Windows).
    assert capsys.readouterr().err == (
        f"slopfence: error: baseline {Path('ci/base.json')} does not exist "
        "(create it with --write-baseline)\n"
    )
    # --no-baseline shows everything again.
    assert main([".", "--no-baseline"]) == 1


def test_baseline_option_replaces_the_config_key(project, capsys):
    """--baseline on the command line is used in place of the configured file."""
    main([".", "--write-baseline", "all.json"])
    main([".", "--select", "SLOP051", "--write-baseline", "one.json"])
    (project / "pyproject.toml").write_text('[tool.slopfence]\nbaseline = "all.json"\n')
    assert report(capsys)[1]["baselined"] == 9
    assert report(capsys, "--baseline", "one.json")[1]["baselined"] == 1


def test_write_baseline_does_not_use_the_configured_baseline(project, capsys):
    """Writing the baseline again starts from every finding, not from what is left."""
    (project / "pyproject.toml").write_text('[tool.slopfence]\nbaseline = "base.json"\n')
    # The configured file isn't there yet: writing it must still work.
    assert main([".", "--write-baseline", "base.json"]) == 0
    first = (project / "base.json").read_bytes()
    assert main([".", "--write-baseline", "base.json"]) == 0
    assert (project / "base.json").read_bytes() == first
    assert len(rows(project / "base.json")) == 9
    # --no-baseline changes nothing here: a run that writes uses no baseline anyway.
    assert main([".", "--no-baseline", "--write-baseline", "base.json"]) == 0
    assert (project / "base.json").read_bytes() == first
    # A normal run before the file exists is an error.
    (project / "base.json").unlink()
    capsys.readouterr()
    assert main(["."]) == 2
    assert "does not exist (create it with --write-baseline)" in capsys.readouterr().err


# --- the config key ---------------------------------------------------------------------


def test_baseline_config_key_is_loaded_as_written(tmp_path):
    """The path is kept as written: the command line knows what it is relative to."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.slopfence]\nbaseline = "ci/base.json"\n', encoding="utf-8"
    )
    assert load_config(tmp_path).baseline == "ci/base.json"
    assert load_config(tmp_path / "missing").baseline is None


@pytest.mark.parametrize("value", ["true", '" "', '""', "3", '["base.json"]'])
def test_invalid_baseline_config(tmp_path, monkeypatch, capsys, value):
    """A baseline key that isn't a path is a config error."""
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.slopfence]\nbaseline = {value}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline"]) == 2
    assert "[tool.slopfence]: 'baseline' must be a file path" in capsys.readouterr().err


# --- duplicates with --diff (a full scan never reports the first copy) ------------------


def helper(name, doc="Format a date."):
    """A function that SLOP030 matches with every other ``helper``, whatever its docstring."""
    return HELPER.format(name=name).replace("\n", f'\n    """{doc}"""\n', 1)


# Only SLOP030 matters in these tests.
ONLY_SLOP030 = ["--offline", "--select", "SLOP030"]


def diff_run(capsys, *args):
    """``--diff main``: the exit code, the path of each finding, and the baselined count."""
    code, after = report(capsys, *ONLY_SLOP030, "--diff", "main", *args)
    return code, [f["path"] for f in after["findings"]], after["baselined"]


def full_run(capsys, *args):
    """A run without ``--diff``: the exit code and the path of each finding."""
    code, after = report(capsys, *ONLY_SLOP030, *args)
    return code, [f["path"] for f in after["findings"]]


def test_touched_first_copy_of_an_accepted_duplicate_stays_hidden(
    write, tmp_path, monkeypatch, capsys
):
    """With --diff a changed first copy is a finding; the entry of its twin stands for it."""
    write("src/dates.py", helper("format_date", "Fromat a date."))
    write("src/utils.py", helper("fmt_date", "Fromat a date."))
    monkeypatch.chdir(tmp_path)
    main([".", *ONLY_SLOP030, "--write-baseline", "base.json"])
    # The full scan reports the second copy only, so only that one is recorded.
    assert rows(tmp_path / "base.json") == [["src/utils.py", "SLOP030", "fmt_date", 1]]
    commit_all(tmp_path)
    git(tmp_path, "switch", "-q", "-c", "typo")
    edit(tmp_path / "src" / "dates.py", "Fromat", "Format")  # the first copy's docstring only

    assert diff_run(capsys) == (1, ["src/dates.py"], 0)  # --diff alone reports it
    assert diff_run(capsys, "--baseline", "base.json") == (0, [], 1)
    # Both copies touched: the second is hidden by its own entry, the first by its twin's.
    edit(tmp_path / "src" / "utils.py", "Fromat", "Format")
    assert diff_run(capsys, "--baseline", "base.json") == (0, [], 2)
    assert full_run(capsys, "--baseline", "base.json") == (0, [])


def test_new_copy_of_an_accepted_duplicate_is_reported_with_diff(
    write, tmp_path, monkeypatch, capsys
):
    """A new copy is reported, also when it sorts first and every other copy is baselined."""
    for name in ("b", "c", "d"):
        write(f"src/{name}.py", helper(f"f{name}"))
    monkeypatch.chdir(tmp_path)
    main([".", *ONLY_SLOP030, "--write-baseline", "base.json"])
    commit_all(tmp_path)
    git(tmp_path, "switch", "-q", "-c", "feature")
    write("src/a.py", helper("fa"))

    # The copy it points at (b.py) was the first one until now: it has no entry.
    assert diff_run(capsys, "--baseline", "base.json") == (1, ["src/a.py"], 0)
    # A full run blames the old first copy in its place, and fails as well.
    assert full_run(capsys, "--baseline", "base.json") == (1, ["src/b.py"])
    # A new copy that sorts last is reported by both, in the same place.
    (tmp_path / "src" / "a.py").unlink()
    write("src/z.py", helper("fz"))
    assert diff_run(capsys, "--baseline", "base.json") == (1, ["src/z.py"], 0)
    assert full_run(capsys, "--baseline", "base.json") == (1, ["src/z.py"])


def test_touched_first_copy_and_a_new_copy_give_one_finding(write, tmp_path, monkeypatch, capsys):
    """Touching the first copy of an accepted pair never lets a new copy slip past --diff."""
    write("src/dates.py", helper("format_date", "Fromat a date."))
    write("src/utils.py", helper("fmt_date"))
    monkeypatch.chdir(tmp_path)
    main([".", *ONLY_SLOP030, "--write-baseline", "base.json"])
    commit_all(tmp_path)
    git(tmp_path, "switch", "-q", "-c", "feature")
    edit(tmp_path / "src" / "dates.py", "Fromat", "Format")
    write("src/aaa_new.py", helper("brand_new_copy"))

    # The same function and the same count as the full run: dates.py is not the first
    # copy any more. The new first copy is hidden by the entry of the accepted twin.
    assert diff_run(capsys, "--baseline", "base.json") == (1, ["src/dates.py"], 1)
    assert full_run(capsys, "--baseline", "base.json") == (1, ["src/dates.py"])


# Two methods that SLOP030 never takes for copies of each other.
METHOD = (
    "    def {name}(self, value, fmt='%Y'):\n        if value is None:\n"
    "            return ''\n        text = value.strftime(fmt)\n        return text.strip()\n"
)
OTHER_METHOD = (
    "    def {name}(self, items, sep=','):\n        if not items:\n"
    "            return ''\n        joined = sep.join(items)\n        return joined.upper()\n"
)


def test_unchanged_duplicates_use_their_baseline_entries_first(tmp_path, monkeypatch, capsys):
    """With --diff an old duplicate keeps its entry; a new one with its name is reported."""
    (tmp_path / "aaa.py").write_text("class Base:\n" + METHOD.format(name="render"))
    (tmp_path / "aab.py").write_text("class Other:\n" + OTHER_METHOD.format(name="join_up"))
    models = tmp_path / "models.py"
    models.write_text("class A:\n" + METHOD.format(name="to_text"))
    monkeypatch.chdir(tmp_path)
    main([".", *ONLY_SLOP030, "--write-baseline", "base.json"])
    assert rows(tmp_path / "base.json") == [["models.py", "SLOP030", "to_text", 1]]
    commit_all(tmp_path)
    git(tmp_path, "switch", "-q", "-c", "feature")
    # A second method named to_text in the same file. It copies another function.
    models.write_text(models.read_text() + "\n\nclass B:\n" + OTHER_METHOD.format(name="to_text"))

    code, after = report(capsys, *ONLY_SLOP030, "--diff", "main", "--baseline", "base.json")
    assert (code, [(f["path"], f["line"]) for f in after["findings"]]) == (1, [("models.py", 10)])
    assert after["baselined"] == 0  # the old one is hidden, but it is not on a changed line
    code, after = report(capsys, *ONLY_SLOP030, "--baseline", "base.json")
    assert (code, [f["line"] for f in after["findings"]], after["baselined"]) == (1, [10], 1)


def test_new_first_copy_cannot_hide_behind_an_entry_of_the_same_name(tmp_path, monkeypatch, capsys):
    """The entry of an old duplicate named like the twin does not stand for a new copy.

    models.py has an accepted duplicate named to_text, and a second to_text that copies
    nothing yet. A new file then copies the second one and sorts first. The old first
    copy is now the one a full scan reports, and --diff must fail as well.
    """
    (tmp_path / "base_model.py").write_text("class Base:\n" + METHOD.format(name="render"))
    (tmp_path / "models.py").write_text(
        "class A:\n"
        + METHOD.format(name="to_text")
        + "\n\nclass B:\n"
        + OTHER_METHOD.format(name="to_text")
    )
    monkeypatch.chdir(tmp_path)
    main([".", *ONLY_SLOP030, "--write-baseline", "base.json"])
    assert rows(tmp_path / "base.json") == [["models.py", "SLOP030", "to_text", 1]]
    commit_all(tmp_path)
    git(tmp_path, "switch", "-q", "-c", "feature")
    (tmp_path / "aaa_new.py").write_text("class New:\n" + OTHER_METHOD.format(name="join_up"))

    assert full_run(capsys, "--baseline", "base.json") == (1, ["models.py"])
    assert diff_run(capsys, "--baseline", "base.json") == (1, ["aaa_new.py"], 0)


def test_new_first_copy_cannot_use_the_entry_of_a_function_it_replaced(
    tmp_path, monkeypatch, capsys
):
    """An entry that a rewritten function left behind does not hide what it is now.

    render was an accepted copy. It is rewritten into a copy of to_text, the function
    below it. A full scan now reports to_text, which has no entry. With --diff the
    finding is on render, the first copy, and the old entry named render must not hide it.
    """
    (tmp_path / "base_model.py").write_text("class Base:\n" + METHOD.format(name="show"))
    models = tmp_path / "models.py"
    models.write_text(
        "class A:\n"
        + METHOD.format(name="render")
        + "\n\nclass B:\n"
        + OTHER_METHOD.format(name="to_text")
    )
    monkeypatch.chdir(tmp_path)
    main([".", *ONLY_SLOP030, "--write-baseline", "base.json"])
    assert rows(tmp_path / "base.json") == [["models.py", "SLOP030", "render", 1]]
    commit_all(tmp_path)
    git(tmp_path, "switch", "-q", "-c", "feature")
    models.write_text(
        "class A:\n"
        + OTHER_METHOD.format(name="render")
        + "\n\nclass B:\n"
        + OTHER_METHOD.format(name="to_text")
    )

    code, after = report(capsys, *ONLY_SLOP030, "--baseline", "base.json")
    assert (code, [(f["path"], f["line"]) for f in after["findings"]]) == (1, [("models.py", 10)])
    code, after = report(capsys, *ONLY_SLOP030, "--diff", "main", "--baseline", "base.json")
    assert (code, [(f["path"], f["line"]) for f in after["findings"]]) == (1, [("models.py", 2)])


def test_peer_entry_hides_a_finding_without_being_used_up():
    """A finding with a peer is hidden when the peer has an entry. The entry stays whole."""
    baseline = Baseline({("b.py", "SLOP030", "fmt_date"): 1})
    first = Finding("SLOP030", "a.py", 1, 1, "m", subject="format_date", peer=("b.py", "fmt_date"))
    own = Finding("SLOP030", "b.py", 1, 1, "m", subject="fmt_date")
    other = Finding("SLOP030", "c.py", 1, 1, "m", subject="x", peer=("d.py", "not_recorded"))
    matched = baselinemod.match([first, own, other], baseline)
    assert (matched.hidden, matched.new, matched.unused) == ([first, own], [other], {})
    # Without the peer's own finding, the entry is still all there afterwards.
    alone = baselinemod.match([first], baseline)
    assert (alone.hidden, alone.unused) == ([first], {("b.py", "SLOP030", "fmt_date"): 1})


def test_finding_with_a_peer_never_uses_an_entry_with_its_own_key():
    """A full scan would not report it, so an entry with its key belongs to another function."""
    baseline = Baseline({("z.py", "SLOP030", "render"): 1})
    first = Finding("SLOP030", "z.py", 2, 1, "m", end_line=6, subject="render", peer=("z.py", "x"))
    matched = baselinemod.match([first], baseline, {"z.py": {3}})
    assert (matched.new, matched.hidden) == ([first], [])
    assert matched.unused == {("z.py", "SLOP030", "render"): 1}


def test_peer_entry_that_is_too_small_for_the_old_findings_hides_nothing_more():
    """One entry, two old findings with its key: one of them is new, so the peer is not hidden."""
    baseline = Baseline({("b.py", "SLOP030", "to_text"): 1})
    first = Finding(
        "SLOP030", "a.py", 1, 1, "m", end_line=5, subject="new", peer=("b.py", "to_text")
    )
    old = Finding("SLOP030", "b.py", 2, 1, "m", end_line=6, subject="to_text")
    newly_blamed = Finding("SLOP030", "b.py", 10, 1, "m", end_line=14, subject="to_text")
    matched = baselinemod.match([first, old, newly_blamed], baseline, {"a.py": None})
    assert (matched.hidden, matched.new) == ([old], [first, newly_blamed])
    # When the second finding with that key is on a changed line, it is reported itself,
    # and the function with the peer stays hidden: the old one it copies is accepted.
    matched = baselinemod.match([first, old, newly_blamed], baseline, {"a.py": None, "b.py": {12}})
    assert (matched.hidden, matched.new) == ([first, old], [newly_blamed])


# Function bodies that SLOP030 never takes for copies of each other.
BODIES = [
    "    if value is None:\n        return ''\n    text = value.strftime(fmt)\n    return text\n",
    "    items = []\n    for part in value:\n        items.append(fmt % part)\n    return items\n",
    "    try:\n        number = int(value)\n    except ValueError:\n        number = 0\n"
    "    return number + len(fmt)\n",
    "    while value and value[-1] in fmt:\n        value = value[:-1]\n    count = len(value)\n"
    "    return count, value\n",
]
FILES = ["a/m1.py", "a/m2.py", "a-b/m3.py", "lib/x.py", "svc/y.py", "svc/z.py", "top.py"]
NAMES = ["render", "to_text", "load", "clean", "fmt", "build"]


def random_function(rng, taken):
    """A function or a method with one of BODIES. Functions get a name the file doesn't have."""
    method = rng.random() < 0.4
    name = rng.choice(NAMES)
    while not method and name in taken:
        name = rng.choice(NAMES) + str(rng.randrange(100))
    if not method:
        taken.add(name)
    return {"name": name, "body": rng.randrange(len(BODIES)), "method": method, "doc": "Do it."}


def module_text(functions):
    """The source of a module with these functions. Each method gets a class of its own."""
    parts = []
    for number, function in enumerate(functions):
        text = f'    """{function["doc"]}"""\n' + BODIES[function["body"]]
        if function["method"]:
            body = "".join(f"    {line}\n" for line in text.splitlines())
            parts.append(
                f"class C{number}:\n    def {function['name']}(self, value, fmt='%Y'):\n{body}"
            )
        else:
            parts.append(f"def {function['name']}(value, fmt='%Y'):\n{text}")
    return "\n\n".join(parts)


def random_pull_request(rng, project):
    """Change ``project`` like a pull request does: edit docstrings, add and delete functions."""
    for _ in range(rng.randint(1, 3)):
        kind = rng.choice(["touch", "touch", "add", "add", "delete"])
        files = [rel for rel, functions in project.items() if functions]
        if kind == "touch" and files:
            rng.choice(project[rng.choice(files)])["doc"] += " Fixed a typo."
        elif kind == "add":
            functions = project.setdefault(rng.choice(FILES), [])
            taken = {f["name"] for f in functions if not f["method"]}
            functions.insert(rng.randint(0, len(functions)), random_function(rng, taken))
        elif kind == "delete" and files:
            functions = project[rng.choice(files)]
            functions.pop(rng.randrange(len(functions)))


def changed_lines(before, after):
    """What ``git diff`` would say changed between two versions of a project's files."""
    changed = {}
    for rel, text in after.items():
        if rel not in before:
            changed[rel] = None
        elif text != before[rel]:
            old, new = before[rel].splitlines(), text.splitlines()
            blocks = difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
            changed[rel] = {
                line + 1 for tag, _, _, j1, j2 in blocks if tag != "equal" for line in range(j1, j2)
            }
    return changed


def duplicates(root, project, **kwargs):
    """Write the files of ``project`` under ``root`` and check them for duplicates."""
    for rel in FILES:
        path = root / rel
        path.unlink(missing_ok=True)
        if project.get(rel):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(module_text(project[rel]), encoding="utf-8")
    return run([root], root, OfflineRegistry(), select=["SLOP030"], **kwargs)


def test_diff_run_and_full_run_agree_about_duplicates(tmp_path):
    """With a baseline, a --diff run fails when a full run fails, and only then.

    150 small random projects with groups of identical functions, some in classes
    and some with the same name in one file. Each gets a baseline, then a random pull
    request. If the full run finds a new duplicate, the pull request added one, and
    --diff has to report something too. If it finds none, --diff must be silent.
    """
    rng = random.Random(51)
    kinds = {True: 0, False: 0}
    for case in range(150):
        root = tmp_path / str(case)
        project = {}
        for rel in rng.sample(FILES, rng.randint(2, 6)):
            taken = set()
            project[rel] = [random_function(rng, taken) for _ in range(rng.randint(1, 3))]
        accepted = Baseline(baselinemod.count_findings(duplicates(root, project).findings))
        before = {rel: module_text(functions) for rel, functions in project.items() if functions}
        random_pull_request(rng, project)
        after = {rel: module_text(functions) for rel, functions in project.items() if functions}
        full = duplicates(root, project, baseline=accepted)
        diff = duplicates(root, project, baseline=accepted, changed=changed_lines(before, after))
        assert bool(diff.findings) == bool(full.findings), (case, full.findings, diff.findings)
        kinds[bool(full.findings)] += 1
    # Both kinds of pull request came up often enough to mean something.
    assert min(kinds.values()) >= 30


# --- a baseline file that can't be used -------------------------------------------------


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "does not exist (create it with --write-baseline)"),
        ("", "is not valid JSON: Expecting value"),
        ("{not json", "is not valid JSON: Expecting property name"),
        ("[" * 100_000, "is not valid JSON: nested too deeply"),
        ("[]", "is not a slopfence baseline file"),
        ('{"findings": []}', "is not a slopfence baseline file"),
        ('{"version": true, "findings": []}', "is not a slopfence baseline file"),
        ('{"version": "1", "findings": []}', "is not a slopfence baseline file"),
        (
            '{"version": 2, "findings": []}',
            "has format version 2, but this slopfence reads version 1; re-create it with "
            "--write-baseline",
        ),
        ('{"version": 1}', "'findings' must be a list of [path, rule, fingerprint, count]"),
        ('{"version": 1, "findings": {}}', "'findings' must be a list of [path, rule,"),
        ('{"version": 1, "findings": [["a.py", "SLOP010", "x"]]}', "'findings' must be a list"),
        ('{"version": 1, "findings": [["a.py", "SLOP010", "x", 0]]}', "'findings' must be a list"),
        ('{"version": 1, "findings": [["a.py", "SLOP010", "x", true]]}', "'findings' must be a"),
        ('{"version": 1, "findings": [["a.py", "SLOP010", 5, 1]]}', "'findings' must be a list"),
    ],
)
def test_missing_or_invalid_baseline_exits_2(
    write, tmp_path, monkeypatch, capsys, content, message
):
    """A baseline that can't be used is an error, never an empty baseline."""
    write("app.py", "# I hope this helps!\n")
    if content is not None:
        (tmp_path / "base.json").write_text(content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(cli, "run", None)  # found before the scan: run() is never called
        assert main([".", "--offline", "--baseline", "base.json"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("slopfence: error: baseline base.json ") and message in err
    assert len(err.splitlines()) == 1  # one line, no traceback


def test_baseline_that_is_not_utf8_exits_2(write, tmp_path, monkeypatch, capsys):
    """Bytes that are not UTF-8 are reported like any other text that is not JSON."""
    write("app.py", "x = 1\n")
    (tmp_path / "base.json").write_bytes(b'{"version": 1, "findings": [["caf\xe9.py"]]}')
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline", "--baseline", "base.json"]) == 2
    assert "slopfence: error: baseline base.json is not valid JSON:" in capsys.readouterr().err


def test_folder_in_place_of_the_baseline_exits_2(write, tmp_path, monkeypatch, capsys):
    """A folder can't be read as a baseline: the system's own reason is printed."""
    write("app.py", "x = 1\n")
    (tmp_path / "adir").mkdir()
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline", "--baseline", "adir"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("slopfence: error: baseline adir: ") and len(err.splitlines()) == 1


def test_exit_zero_does_not_hide_a_baseline_error(write, tmp_path, monkeypatch):
    """--exit-zero is about findings. A baseline that can't be read still stops the run."""
    write("app.py", "x = 1\n")
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline", "--exit-zero", "--baseline", "missing.json"]) == 2


def test_options_that_cannot_be_combined(tmp_path, capsys):
    """--write-baseline with --diff or --baseline, and --baseline with --no-baseline."""
    cases = [
        (["--write-baseline", "w.json", "--diff", "main"], "so it can't be used with --diff"),
        (
            ["--write-baseline", "w.json", "--baseline", "b.json"],
            "--write-baseline can't be used with --baseline",
        ),
        (["--baseline", "b.json", "--no-baseline"], "not allowed with argument --baseline"),
    ]
    for options, message in cases:
        with pytest.raises(SystemExit) as err:
            main([str(tmp_path), *options])
        assert err.value.code == 2
        assert message in capsys.readouterr().err
    assert not (tmp_path / "w.json").exists()


def test_odd_paths_in_a_baseline_never_crash(write, tmp_path, monkeypatch, capsys):
    """A row is only data: a path the file system turns down is a file that isn't there."""
    write("app.py", "x = 1\n")
    odd = [
        ["a" * 5000 + ".py", "SLOP010", "x", 1],
        ["nul\x00byte.py", "SLOP010", "x", 1],
        ["../outside/gone.py", "SLOP010", "x", 2],
    ]
    (tmp_path / "base.json").write_text(json.dumps({"version": 1, "findings": odd}))
    monkeypatch.chdir(tmp_path)
    code, after = report(capsys, "--offline", "--baseline", "base.json")
    assert (code, after["notes"]) == (0, [GONE.format("4 findings", "exist")])
    # Writing over it works too: none of those files exists, so nothing is lost.
    assert main([".", "--offline", "--write-baseline", "base.json"]) == 0
    assert rows(tmp_path / "base.json") == []


# --- the file that --write-baseline replaces --------------------------------------------


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("src/app.py", b"def f():\n    return 1\n"),
        ("data.json", b"[1, 2]"),
        ("package.json", b'{"version": "1.0.0", "findings": []}'),
        ("other.json", b'{"version": 3}'),
        ("latin.txt", b"caf\xe9"),
        ("deep.json", b"[" * 100_000),
    ],
)
def test_write_baseline_never_replaces_a_file_that_is_not_a_baseline(
    tmp_path, monkeypatch, capsys, name, content
):
    """A forgotten file name (--write-baseline src/app.py) must not destroy that file."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_bytes(b"def f():\n    return 1\n")
    (tmp_path / name).write_bytes(content)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "run", None)  # refused before the scan: run() is never called
    assert main(["--offline", "--write-baseline", name]) == 2
    assert capsys.readouterr().err == (
        f"slopfence: error: can't write baseline {Path(name)}: the file exists and is not a "
        "slopfence baseline (delete it first, or choose another name)\n"
    )
    assert (tmp_path / name).read_bytes() == content


@pytest.mark.parametrize(
    "content",
    [
        "",
        "\n",
        '{"version": 2, "findings": [{"new": "format"}]}',
        '{"version": 1, "findings": [["damaged row"]]}',
        '{"version": 1, "findings": [["gone.py", "SLOP010", "x", 1]]}',
    ],
)
def test_write_baseline_replaces_empty_files_and_old_baselines(
    write, tmp_path, monkeypatch, content
):
    """An empty file and any slopfence baseline may be replaced."""
    write("app.py", "# I hope this helps!\n")
    (tmp_path / "base.json").write_text(content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline", "--write-baseline", "base.json"]) == 0
    assert baselinemod.load(tmp_path / "base.json").entries == {
        ("app.py", "SLOP051", "I hope this helps!"): 1
    }


def test_bad_write_targets_are_refused_before_the_scan(tmp_path, monkeypatch, capsys):
    """A folder, an empty name or a missing folder never costs a full run."""
    (tmp_path / "adir").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "run", None)
    for target, reason in [
        ("adir", "it is a folder"),
        ("", "it is a folder"),
        ("missing/base.json", "the folder missing does not exist"),
    ]:
        assert main([".", "--offline", "--write-baseline", target]) == 2
        assert capsys.readouterr().err == (
            f"slopfence: error: can't write baseline {Path(target)}: {reason}\n"
        )
    assert sorted(p.name for p in tmp_path.iterdir()) == ["adir"]


def test_write_target_that_cannot_be_read_is_refused(write, tmp_path, monkeypatch, capsys):
    """Nobody can say what an unreadable file holds, so it is not replaced."""
    write("app.py", "x = 1\n")
    write("base.json", "{}")

    def denied(path):
        """Stand in for a file this user may not read."""
        raise PermissionError(13, "Permission denied")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(baselinemod, "_read_text", denied)
    monkeypatch.setattr(cli, "run", None)  # refused before the scan: run() is never called
    assert main([".", "--offline", "--write-baseline", "base.json"]) == 2
    assert capsys.readouterr().err == (
        "slopfence: error: can't write baseline base.json: Permission denied\n"
    )


def test_write_baseline_refuses_to_drop_findings_outside_the_checked_paths(
    project, capsys, monkeypatch
):
    """Writing from a sub-folder must not replace the baseline of the whole project."""
    main([".", "--write-baseline", "base.json"])
    before = (project / "base.json").read_bytes()
    capsys.readouterr()
    with monkeypatch.context() as patch:
        patch.setattr(cli, "run", None)  # refused before the scan: run() is never called
        assert main(["src", "--write-baseline", "base.json"]) == 2
    assert capsys.readouterr().err == (
        "slopfence: error: can't write baseline base.json: it holds 4 findings for files "
        "outside the checked paths (for example requirements.txt). Check the whole project, "
        "or delete the file first\n"
    )
    assert (project / "base.json").read_bytes() == before
    # Another file name is fine. So is the same name once those files are gone.
    assert main(["src", "--write-baseline", "src-only.json"]) == 0
    (project / "requirements.txt").unlink()
    (project / "tests" / "test_app.py").unlink()
    assert main(["src", "--write-baseline", "base.json"]) == 0
    assert {row[0] for row in rows(project / "base.json")} == {"src/app.py", "src/utils.py"}
    # And naming every path the baseline has findings for is the same as naming the project.
    assert main(["src/app.py", "src/utils.py", "--write-baseline", "base.json"]) == 0


def test_baseline_can_be_written_again_for_a_path_outside_the_project(
    write, tmp_path, monkeypatch, capsys
):
    """A file outside the project is named by its absolute path, in the run and in the file."""
    write("proj/app.py", "# I hope this helps!\n")
    other = write("other/lib.py", "# I hope this helps!\n")
    monkeypatch.chdir(tmp_path / "proj")
    assert main([".", "../other", "--offline", "--write-baseline", "base.json"]) == 0
    assert [row[0] for row in rows(tmp_path / "proj" / "base.json")] == [
        other.resolve().as_posix(),
        "app.py",
    ]
    # The second write finds a baseline with a finding for ../other, which this run checks.
    assert main([".", "../other", "--offline", "--write-baseline", "base.json"]) == 0
    capsys.readouterr()
    assert main([".", "../other", "--offline", "--no-color", "--baseline", "base.json"]) == 0
    assert capsys.readouterr().out == "No new issues found in 2 files; 2 hidden by the baseline.\n"
    # Without ../other, its finding would be lost.
    assert main([".", "--offline", "--write-baseline", "base.json"]) == 2


def test_failed_write_keeps_the_old_baseline(write, tmp_path, monkeypatch, capsys):
    """The baseline is replaced in one step: a write that fails leaves the old file whole."""
    write("app.py", "# I hope this helps!\n")
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline", "--write-baseline", "base.json"]) == 0
    before = (tmp_path / "base.json").read_bytes()
    write("app.py", "# I hope this helps!\n# Certainly! Here is the code\n")

    def fail(src, dst):
        """Stand in for a replace that the operating system turns down."""
        raise PermissionError(13, "Permission denied")

    capsys.readouterr()
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail)
        assert main([".", "--offline", "--write-baseline", "base.json"]) == 2
    captured = capsys.readouterr()
    assert captured.err == "slopfence: error: can't write baseline base.json: Permission denied\n"
    assert captured.out == ""  # no report that says the findings were written
    assert (tmp_path / "base.json").read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["app.py", "base.json"]
    assert main([".", "--offline", "--write-baseline", "base.json"]) == 0
    assert len(rows(tmp_path / "base.json")) == 2


def test_write_baseline_follows_a_symbolic_link(write, tmp_path, monkeypatch):
    """A baseline that is a link to a shared file stays a link."""
    write("app.py", "# I hope this helps!\n")
    (tmp_path / "shared").mkdir()
    try:
        (tmp_path / "base.json").symlink_to(tmp_path / "shared" / "real.json")
    except OSError:  # Windows without the right to make links
        pytest.skip("symbolic links are not available")
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline", "--write-baseline", "base.json"]) == 0
    assert (tmp_path / "base.json").is_symlink()
    assert len(baselinemod.load(tmp_path / "shared" / "real.json").entries) == 1
    assert sorted(p.name for p in (tmp_path / "shared").iterdir()) == ["real.json"]


def test_write_baseline_with_strict_and_a_file_that_cannot_be_parsed(write, tmp_path, monkeypatch):
    """A file that can't be parsed still fails a --strict run; the baseline is written."""
    write("broken.py", "def broken(:\n")
    write("app.py", "# I hope this helps!\n")
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline", "--strict", "--write-baseline", "base.json"]) == 2
    assert rows(tmp_path / "base.json") == [["app.py", "SLOP051", "I hope this helps!", 1]]
    assert main([".", "--offline", "--write-baseline", "base.json"]) == 0


# --- a baseline written without answers from PyPI ---------------------------------------

MAY_BE_MISSING = (
    "SLOP001 findings may be missing from the baseline: PyPI was not asked, or did not "
    "answer, about some packages (offline, a private package index, or PyPI could not be "
    "reached). Write it again when lookups work."
)


def test_write_baseline_says_when_package_findings_may_be_missing(project, capsys, monkeypatch):
    """A baseline written without answers from PyPI lacks SLOP001 findings; the report says so."""

    def notes(*args):
        """The notes of a run that writes a baseline with the given options."""
        return report(capsys, "--write-baseline", "base.json", *args)[1]["notes"]

    assert notes() == ["Wrote 9 findings to the baseline base.json."]  # every lookup answered
    assert notes("--offline") == ["Wrote 8 findings to the baseline base.json.", MAY_BE_MISSING]
    # Nothing to say when the rule is off, or when there is no name to look up.
    assert notes("--offline", "--ignore", "SLOP001") == [
        "Wrote 8 findings to the baseline base.json."
    ]
    assert notes("--offline", "--exclude", "requirements.txt") == [
        "Wrote 8 findings to the baseline base.json."
    ]
    # A private index in the environment: the note that was there explains, this one warns.
    monkeypatch.setenv("PIP_INDEX_URL", "https://pypi.corp.example/simple")
    with_index = notes()
    assert with_index[0].startswith("SLOP001 made no PyPI lookups: a private package index")
    assert with_index[1:] == ["Wrote 8 findings to the baseline base.json.", MAY_BE_MISSING]
    assert "corp.example" not in " ".join(with_index)  # an index URL is never printed
    monkeypatch.delenv("PIP_INDEX_URL")
    # A run that uses a baseline never prints it.
    main([".", "--write-baseline", "base.json"])
    assert report(capsys, "--offline", "--baseline", "base.json")[1]["notes"] == []


@pytest.mark.parametrize(
    "unanswered", [{"requests", "flask-jwt-simple-auth"}, {"flask-jwt-simple-auth"}]
)
def test_write_baseline_says_when_pypi_did_not_answer(project, capsys, monkeypatch, unanswered):
    """PyPI out of reach, or no answer for one name only: its finding may be missing."""
    (project / "cache" / "slopfence" / "pypi.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        PyPIRegistry, "_query", lambda self, key: None if key in unanswered else key == "requests"
    )
    code, after = report(capsys, "--write-baseline", "base.json")
    assert code == 0
    assert after["notes"] == ["Wrote 8 findings to the baseline base.json.", MAY_BE_MISSING]


def test_result_names_the_package_rules_that_ran_without_answers(write, check):
    """``Result.unchecked_rules`` is for any caller: a report can say what it could not check."""

    class Silent(FakeRegistry):
        """A registry that has no answer for one package."""

        def exists(self, name):
            """Unknown for ``no-answer-zz``; the usual answer for every other name."""
            return None if name == "no-answer-zz" else super().exists(name)

    write("requirements.txt", "requests\n")
    write("app.py", "import os\n")
    assert check(registry=FakeRegistry({"requests"})).unchecked_rules == []
    assert check().unchecked_rules == ["SLOP001"]  # offline: nobody was asked
    assert check("SLOP010").unchecked_rules == []  # the package rule did not run
    assert check(registry=FakeRegistry({"requests"}), private_index="pip.conf").unchecked_rules == [
        "SLOP001"
    ]
    write("requirements.txt", "requests\nno-answer-zz\n")
    assert check(registry=Silent({"requests"})).unchecked_rules == ["SLOP001"]
    # A name that was never asked about (the file is ignored for SLOP001) leaves nothing open.
    quiet = check(registry=Silent({"requests"}), per_file_ignores={"requirements.txt": ["SLOP001"]})
    assert quiet.unchecked_rules == []


def test_offline_import_checks_count_as_unanswered(write, check):
    """Offline, no import is looked up: with check-imports there may be findings it missed."""
    write("app.py", "import made_up_zz\n")
    assert check("SLOP001").unchecked_rules == []  # imports are not checked: nothing to miss
    assert check("SLOP001", check_imports=True).unchecked_rules == ["SLOP001"]


# --- baselined findings that are gone ---------------------------------------------------


def test_gone_findings_are_only_reported_when_the_run_could_find_them(project, capsys):
    """Fixed findings are pointed out. Skipped files, skipped rules and SLOP001 are not."""
    main([".", "--write-baseline", "base.json"])
    # Offline: SLOP001 finds nothing, but its row must not look fixed.
    code, after = report(capsys, "--offline", "--baseline", "base.json")
    assert (code, after["notes"], after["baselined"]) == (0, [], 8)
    # A file that is excluded is not checked: its rows say nothing.
    code, after = report(capsys, "--exclude", "tests", "--baseline", "base.json")
    assert (code, after["notes"], after["baselined"]) == (0, [], 6)
    # Nor do the rows of a file that can't be parsed any more.
    app = (project / "src" / "app.py").read_text(encoding="utf-8")
    (project / "src" / "app.py").write_text("def broken(:\n", encoding="utf-8")
    code, after = report(capsys, "--baseline", "base.json")
    assert (code, after["notes"], after["baselined"]) == (0, [], 5)
    (project / "src" / "app.py").write_text(app, encoding="utf-8")
    # A fixed test file (3 findings) and a deleted file (1).
    (project / "tests" / "test_app.py").write_text("def test_ok():\n    assert compute() == 2\n")
    (project / "src" / "utils.py").unlink()
    code, after = report(capsys, "--baseline", "base.json")
    assert (code, after["notes"]) == (0, [GONE.format("4 findings", "exist")])
    # The text report prints it as a note, and the exit code stays 0.
    assert main([".", "--no-color", "--baseline", "base.json"]) == 0
    assert "note: 4 findings in the baseline no longer exist." in capsys.readouterr().out


def test_missing_import_is_never_called_gone(write, check):
    """SLOP001 for an import depends on PyPI too: no answer must not look like a fix."""
    write("app.py", "import made_up_zz\n")
    found = check("SLOP001", registry=FakeRegistry(set()), check_imports=True)
    assert [f.subject for f in found.findings] == ["made_up_zz"]
    baseline = Baseline(baselinemod.count_findings(found.findings))
    # Offline now. The file is checked and the rule runs, but nothing is found.
    offline = check("SLOP001", check_imports=True, baseline=baseline)
    assert (offline.findings, offline.baselined, offline.notes) == ([], [], [])


def test_nothing_is_called_gone_with_diff(write, tmp_path, monkeypatch, capsys):
    """With --diff some rules only look at changed code, so no entry is ever called gone."""
    write("app.py", "# I hope this helps!\n\nx = 1\n")
    monkeypatch.chdir(tmp_path)
    main([".", "--offline", "--write-baseline", "base.json"])
    commit_all(tmp_path)
    git(tmp_path, "switch", "-q", "-c", "feature")
    write("app.py", "x = 2\n")  # the comment is gone, and so is the finding
    code, after = report(capsys, "--offline", "--diff", "main", "--baseline", "base.json")
    assert (code, after["findings"], after["baselined"], after["notes"]) == (0, [], 0, [])
    # The full run is the one that says so.
    code, after = report(capsys, "--offline", "--baseline", "base.json")
    assert (code, after["notes"]) == (0, [GONE.format("1 finding", "exists")])


def test_gone_note_needs_a_run_that_covers_the_baseline(project, capsys):
    """The advice to write the baseline again is only given when that would lose nothing."""
    main([".", "--write-baseline", "base.json"])
    edit(
        project / "src" / "app.py",
        "    # In a real implementation, call the payment provider\n",
        "",
    )
    note = GONE.format("1 finding", "exists")

    def notes(*paths):
        """The notes of a run on the given paths."""
        capsys.readouterr()
        assert main([*paths, "--format", "json", "--baseline", "base.json"]) == 0
        return json.loads(capsys.readouterr().out)["notes"]

    # src/ alone: the baseline also holds findings for tests/ and requirements.txt.
    assert notes("src") == []
    assert notes("src/app.py") == []
    # Every file the baseline names is inside the given paths: now it is safe to say.
    assert notes("src", "tests", "requirements.txt") == [note]
    assert notes(".") == [note]


def test_part_of_a_project_does_not_make_duplicates_look_gone(write, tmp_path, monkeypatch, capsys):
    """A run on one folder finds the same duplicates in it as the full run, so none is "gone"."""
    for rel in ["lib/a.py", "svc/b.py", "svc/c.py", "svc/d.py", "svc/e.py"]:
        write(rel, HELPER.format(name="helper"))
    monkeypatch.chdir(tmp_path)
    main([".", "--offline", "--write-baseline", "base.json"])
    # Every row is for a file in svc/ (the first copy, in lib/, has none), so a run on
    # svc/ covers the baseline. All four rows are used.
    assert {row[0].split("/")[0] for row in rows(tmp_path / "base.json")} == {"svc"}
    capsys.readouterr()
    assert main(["svc", "--offline", "--format", "json", "--baseline", "base.json"]) == 0
    after = json.loads(capsys.readouterr().out)
    assert (after["baselined"], after["notes"]) == (4, [])


# --- the file format --------------------------------------------------------------------


def test_baseline_file_is_sorted_stable_and_ascii(project):
    """One finding per line, sorted, ASCII, "\\n" line ends: the same bytes everywhere."""
    (project / "src" / "caf\u00e9.py").write_text("# I hope this helps! \u2705\n", encoding="utf-8")
    main([".", "--write-baseline", "base.json"])
    raw = (project / "base.json").read_bytes()
    assert raw.isascii() and b"\r" not in raw and raw.endswith(b"]\n}\n")
    text = raw.decode("ascii")
    assert text.startswith(
        '{\n  "version": 1,\n  "findings": [\n    ["requirements.txt", "SLOP001"'
    )
    found = json.loads(text)["findings"]
    assert found == sorted(found)
    assert len(text.splitlines()) == len(found) + 5
    assert found == [
        ["requirements.txt", "SLOP001", "flask-jwt-simple-auth", 1],
        ["src/app.py", "SLOP010", "In a real implementation, call the payment provider", 1],
        ["src/app.py", "SLOP011", "validate_token", 1],
        ["src/app.py", "SLOP040", "load", 1],
        ["src/app.py", "SLOP051", "I hope this helps!", 1],
        ["src/caf\u00e9.py", "SLOP051", "I hope this helps! \u2705", 1],
        ["src/utils.py", "SLOP030", "fmt_date", 1],
        ["tests/test_app.py", "SLOP020", "test_user", 1],
        ["tests/test_app.py", "SLOP021", "test_nothing", 1],
        ["tests/test_app.py", "SLOP022", "test_never_fails", 1],
    ]
    # The non-ASCII path and text are written as escapes, and come back whole.
    assert "caf\\u00e9.py" in text and "\\u2705" in text


def test_baseline_is_the_same_with_any_number_of_jobs(project, monkeypatch):
    """The file doesn't depend on how many processes checked the files."""
    main([".", "--jobs", "1", "--write-baseline", "one.json"])
    monkeypatch.setattr("slopfence.analysis.MIN_FILES_PER_WORKER", 1)  # 4 files: 3 workers
    main([".", "--jobs", "3", "--write-baseline", "three.json"])
    assert (project / "one.json").read_bytes() == (project / "three.json").read_bytes()


def test_empty_baseline_and_round_trip(tmp_path):
    """An empty baseline is a valid file, and load() reads back what write() wrote."""
    assert baselinemod.dumps({}) == '{\n  "version": 1,\n  "findings": []\n}\n'
    findings = [
        Finding("SLOP011", "a.py", 3, 5, "msg", subject="f"),
        Finding("SLOP011", "a.py", 9, 5, "msg", subject="f"),
        Finding("SLOP010", "a.py", 1, 1, "msg", subject="x  y\tz"),
    ]
    path = tmp_path / "base.json"
    baselinemod.write(path, findings)
    # Sorted by file, rule and fingerprint, whatever order the findings came in.
    assert rows(path) == [["a.py", "SLOP010", "x y z", 1], ["a.py", "SLOP011", "f", 2]]
    loaded = baselinemod.load(path)
    assert loaded.entries == {("a.py", "SLOP010", "x y z"): 1, ("a.py", "SLOP011", "f"): 2}
    assert baselinemod.dumps(loaded.entries) == path.read_text(encoding="utf-8")
    baselinemod.write(path, [])
    assert baselinemod.load(path) == Baseline({})


def test_load_accepts_bom_crlf_repeated_rows_and_unknown_rules(tmp_path):
    """A file that a Windows editor saved, or that was merged by hand, still loads."""
    path = tmp_path / "base.json"
    text = (
        '{"version": 1, "findings": [\r\n["a.py", "SLOP010", "x", 1],\r\n'
        '["a.py", "SLOP010", "x", 2],\r\n["a.py", "SLOP999", "y", 1]]}\r\n'
    )
    path.write_bytes(b"\xef\xbb\xbf" + text.encode())
    assert baselinemod.load(path).entries == {
        ("a.py", "SLOP010", "x"): 3,
        ("a.py", "SLOP999", "y"): 1,  # a rule this version doesn't know: kept
    }
    with pytest.raises(BaselineError, match="does not exist"):
        baselinemod.load(tmp_path / "nope.json")


def test_row_for_an_unknown_rule_matches_nothing_and_is_never_gone(write, check):
    """A baseline from a newer slopfence, with a rule this one lacks, breaks nothing."""
    write("app.py", "# I hope this helps!\n")
    baseline = Baseline({("app.py", "SLOP999", "I hope this helps!"): 1})
    result = check(baseline=baseline)
    assert ([f.rule for f in result.findings], result.baselined, result.notes) == (
        ["SLOP051"],
        [],
        [],
    )


# --- fingerprints -----------------------------------------------------------------------


def test_fingerprint_normalization():
    """Whitespace is collapsed; very long text is cut and ends in a hash of all of it."""
    assert normalize_fingerprint("  a \t b\n") == "a b"
    assert normalize_fingerprint("y" * 120) == "y" * 120
    long_a, long_b = "x" * 200 + "a", "x" * 200 + "b"
    short_a, short_b = normalize_fingerprint(long_a), normalize_fingerprint(long_b)
    assert len(short_a) == 120 and short_a.startswith("x" * 105 + "...")
    assert short_a != short_b
    # Text that can't be written as UTF-8 (half of a surrogate pair) is no error.
    assert len(normalize_fingerprint("\udc80" * 200)) == 120


def test_finding_without_a_subject_falls_back_on_its_message():
    """A finding built without a subject gets a key that can't hide another finding."""
    finding = Finding("SLOP040", "pkg/app.py", 2, 5, "'except Exception'  swallows\tevery error")
    assert key_of(finding) == ("pkg/app.py", "SLOP040", "'except Exception' swallows every error")
    # The subject wins over the message, also when it is empty.
    named = Finding("SLOP040", "pkg/app.py", 2, 5, "message", subject="load")
    assert key_of(named) == ("pkg/app.py", "SLOP040", "load")
    assert key_of(Finding("SLOP040", "pkg/app.py", 2, 5, "message", subject=""))[2] == ""


def test_module_level_finding_stays_hidden_when_its_line_changes(
    write, tmp_path, monkeypatch, capsys
):
    """SLOP040 outside a function is about "<module>", not about the text of its line."""
    write("app.py", "try:\n    import_plugins()\nexcept Exception:\n    pass\n")
    monkeypatch.chdir(tmp_path)
    main([".", "--offline", "--write-baseline", "base.json"])
    assert rows(tmp_path / "base.json") == [["app.py", "SLOP040", "<module>", 1]]
    # Lines added above it, and the handler written another way: still the same finding.
    write("app.py", "import os\n\ntry:\n    import_plugins()\nexcept  ( Exception ) :\n    ...\n")
    code, after = report(capsys, "--offline", "--baseline", "base.json")
    assert (code, after["baselined"]) == (0, 1)


def test_two_imports_of_one_missing_module_share_a_row(write, check):
    """Each import of the module is a finding; the baseline counts them in one row."""
    write("app.py", "import made_up_zz\nfrom made_up_zz.core import x\n")
    result = check("SLOP001", registry=FakeRegistry(set()), check_imports=True)
    assert baselinemod.count_findings(result.findings) == {("app.py", "SLOP001", "made_up_zz"): 2}


# --- the parts, used as a library -------------------------------------------------------


def test_run_with_a_baseline_fills_result_baselined(write, check):
    """run(baseline=...) splits the findings; match() says which entries nothing used."""
    write("app.py", "# I hope this helps!\n# Certainly! Here is the code\n")
    baseline = Baseline(
        {("app.py", "SLOP051", "I hope this helps!"): 1, ("x.py", "SLOP051", "y"): 2}
    )
    result = check(baseline=baseline)
    assert [f.line for f in result.findings] == [2]
    assert [f.line for f in result.baselined] == [1]
    assert result.notes == [GONE.format("2 findings", "exist")]
    matched = baselinemod.match(result.baselined + result.findings, baseline)
    assert (matched.hidden, matched.new) == (result.baselined, result.findings)
    assert matched.unused == {("x.py", "SLOP051", "y"): 2}


def test_match_takes_findings_off_changed_lines_first():
    """With ``changed``, the finding that is not on a changed line uses the entry."""
    first = Finding("SLOP040", "a.py", 4, 5, "m", end_line=5, subject="sync")
    second = Finding("SLOP040", "a.py", 8, 5, "m", end_line=9, subject="sync")
    baseline = Baseline({("a.py", "SLOP040", "sync"): 1})
    assert baselinemod.match([first, second], baseline).new == [second]
    assert baselinemod.match([first, second], baseline, {"a.py": {4}}).new == [first]
    assert baselinemod.match([first, second], baseline, {"a.py": {9}}).new == [second]
    # A file that is new as a whole: both are on changed lines, so the order decides.
    assert baselinemod.match([first, second], baseline, {"a.py": None}).new == [second]


def test_scope_of_names_the_paths_of_a_run(tmp_path, monkeypatch):
    """Paths are named the way findings name their files: "." is the project itself."""
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    assert baselinemod.scope_of([root], root) == ["."]
    paths = [root / "src", root / "src", root / "a.py"]
    assert baselinemod.scope_of(paths, root) == ["a.py", "src"]
    # A path outside the project is absolute, however it was typed.
    monkeypatch.chdir(root)
    outside = (tmp_path / "other").resolve().as_posix()
    assert baselinemod.scope_of([Path("../other")], root) == [outside]
    assert baselinemod.scope_of([Path("src/.."), Path("./src")], root) == [".", "src"]


def test_uncovered_lists_findings_of_existing_files_outside_the_scope(write, tmp_path):
    """Only files that exist and that the run doesn't check stand in the way."""
    root = tmp_path / "proj"
    for rel in ["src/a.py", "srcs/b.py", "tests/c.py"]:
        write(f"proj/{rel}", "x = 1\n")
    entries = {
        ("src/a.py", "SLOP010", "x"): 1,
        ("srcs/b.py", "SLOP010", "x"): 2,  # "src" is not a part of "srcs"
        ("srcs/b.py", "SLOP011", "f"): 1,
        ("tests/c.py", "SLOP010", "x"): 1,
        ("tests/gone.py", "SLOP010", "x"): 5,  # no such file: nothing to lose
    }
    assert baselinemod.uncovered(entries, ["src"], root) == {"srcs/b.py": 3, "tests/c.py": 1}
    assert baselinemod.uncovered(entries, ["src", "srcs/b.py", "tests"], root) == {}
    assert baselinemod.uncovered(entries, ["."], root) == {}
    # A file outside the project has an absolute path. The project does not hold it.
    elsewhere = write("elsewhere/d.py", "x = 1\n").resolve().as_posix()
    entries[(elsewhere, "SLOP010", "x")] = 2
    assert baselinemod.uncovered(entries, ["."], root) == {elsewhere: 2}
    assert baselinemod.uncovered(entries, [".", elsewhere.rsplit("/", 1)[0]], root) == {}


def test_count_stale_counts_what_the_run_should_have_found(write, tmp_path):
    """An unused entry counts when its rule ran and its file was checked or is gone."""
    write("a.py", "x = 1\n")
    write("skipped.py", "x = 1\n")
    unused = {
        ("a.py", "SLOP010", "x"): 2,  # checked, the rule ran: gone
        ("a.py", "SLOP001", "pkg"): 1,  # the rule is not one to trust
        ("skipped.py", "SLOP010", "x"): 1,  # the file is there but was not checked
        ("deleted.py", "SLOP010", "x"): 4,  # the file is gone: so are its findings
    }
    assert baselinemod.count_stale(unused, {"SLOP010"}, {"a.py"}, ["."], tmp_path) == 6
    assert (
        baselinemod.count_stale(unused, {"SLOP010"}, {"a.py"}, ["a.py", "skipped.py"], tmp_path)
        == 6
    )
    # skipped.py is outside the paths of the run: the run does not cover the baseline.
    assert baselinemod.count_stale(unused, {"SLOP010"}, {"a.py"}, ["a.py"], tmp_path) == 0
    assert baselinemod.count_stale({}, {"SLOP010"}, {"a.py"}, ["."], tmp_path) == 0
