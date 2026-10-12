import json

import pytest

from slopfence import diff as diffmod
from slopfence.cli import main
from tests.conftest import git as _git

SLOPPY = """\
def charge(card):
    # In a real implementation, call the payment provider
    return True
"""


def test_exit_codes(tmp_path, capsys):
    (tmp_path / "clean.py").write_text("x = 1\n")
    assert main([str(tmp_path), "--offline"]) == 0
    (tmp_path / "app.py").write_text(SLOPPY)
    assert main([str(tmp_path), "--offline"]) == 1
    assert main([str(tmp_path), "--offline", "--exit-zero"]) == 0
    assert main([str(tmp_path / "missing")]) == 2
    out = capsys.readouterr().out
    assert "SLOP010" in out
    assert "1 issue (1 medium)" in out


def test_select_and_ignore(tmp_path, capsys):
    (tmp_path / "app.py").write_text(SLOPPY + "# I hope this helps!\n")
    main([str(tmp_path), "--offline", "--format", "json", "--select", "SLOP051"])
    rules = {f["rule"] for f in json.loads(capsys.readouterr().out)["findings"]}
    assert rules == {"SLOP051"}
    main([str(tmp_path), "--offline", "--format", "json", "--ignore", "slop051"])
    rules = {f["rule"] for f in json.loads(capsys.readouterr().out)["findings"]}
    assert rules == {"SLOP010"}


def test_unknown_rule_is_an_error(tmp_path):
    with pytest.raises(SystemExit):
        main([str(tmp_path), "--select", "SLOP999"])


def test_sarif_output(tmp_path):
    (tmp_path / "app.py").write_text(SLOPPY)
    out = tmp_path / "report.sarif"
    main([str(tmp_path), "--offline", "--format", "sarif", "-o", str(out)])
    doc = json.loads(out.read_text())
    assert doc["version"] == "2.1.0"
    run = doc["runs"][0]
    assert run["tool"]["driver"]["name"] == "slopfence"
    [res] = run["results"]
    assert res["ruleId"] == "SLOP010"
    assert res["level"] == "warning"
    loc = res["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "app.py"
    assert loc["region"]["startLine"] == 2


def test_parse_unified_diff():
    text = """\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,0 +2,3 @@ def f():
+a
+b
+c
@@ -10 +14 @@
+d
diff --git a/old.py b/old.py
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
"""
    changed = diffmod.parse_unified_diff(text)
    assert changed == {"app.py": {2, 3, 4, 14}}
    assert diffmod.touches(changed, "app.py", 1, 2)
    assert not diffmod.touches(changed, "app.py", 5, 13)
    assert not diffmod.touches(changed, "other.py", 1, None)


def test_diff_mode_only_reports_changed_lines(tmp_path, capsys, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "old.py").write_text("# In a real implementation, do X\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _git(tmp_path, "switch", "-q", "-c", "feature")
    (tmp_path / "old.py").write_text(
        "# In a real implementation, do X\nx = 1\n# I hope this helps!\n"
    )
    (tmp_path / "new.py").write_text("# Simplified implementation for demo\n")

    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline", "--diff", "main", "--format", "json"]) == 1
    findings = json.loads(capsys.readouterr().out)["findings"]
    assert sorted((f["path"], f["line"], f["rule"]) for f in findings) == [
        ("new.py", 1, "SLOP010"),
        ("old.py", 3, "SLOP051"),
    ]


def test_diff_with_bad_ref(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path)
    assert main([".", "--offline", "--diff", "no-such-ref"]) == 2


def test_list_rules(capsys):
    assert main(["--list-rules"]) == 0
    assert "SLOP001" in capsys.readouterr().out


def test_unquote_git_path():
    assert diffmod.unquote_git_path('"b/caf\\303\\251.py"') == "b/café.py"
    assert diffmod.unquote_git_path('"b/a\\tb.py"') == "b/a\tb.py"
    assert diffmod.unquote_git_path("b/plain.py") == "b/plain.py"


def test_diff_mode_with_non_ascii_filenames(tmp_path, capsys, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "café.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _git(tmp_path, "switch", "-q", "-c", "feature")
    (tmp_path / "café.py").write_text("x = 1\n# I hope this helps!\n")
    (tmp_path / "naïve.py").write_text("# I hope this helps!\n")
    monkeypatch.chdir(tmp_path)
    main([".", "--offline", "--diff", "main", "--format", "json"])
    findings = json.loads(capsys.readouterr().out)["findings"]
    assert sorted((f["path"], f["line"]) for f in findings) == [("café.py", 2), ("naïve.py", 1)]


def test_sarif_uri_is_percent_encoded(tmp_path):
    (tmp_path / "task#1 50%.py").write_text(SLOPPY)
    out = tmp_path / "r.sarif"
    main([str(tmp_path), "--offline", "--format", "sarif", "-o", str(out)])
    [res] = json.loads(out.read_text())["runs"][0]["results"]
    uri = res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert uri == "task%231%2050%25.py"


def test_excluded_directories_are_not_walked(tmp_path):
    from slopfence.engine import discover_all

    (tmp_path / "app.py").write_text("")
    for d in [".venv/lib", "node_modules/x", "pkg.egg-info"]:
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "m.py").write_text("")
    py_files, _, _ = discover_all([tmp_path])
    assert [p.name for p in py_files] == ["app.py"]


def test_non_utf8_stdout_does_not_crash(tmp_path, monkeypatch):
    # Windows CI pipes default to cp1252, which can't encode emoji.
    import io
    import sys

    (tmp_path / "app.py").write_text("# I hope this helps! \u2705 caf\u00e9\n", encoding="utf-8")
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    assert main([str(tmp_path), "--offline", "--no-color"]) == 1
    sys.stdout.flush()
    assert "I hope this helps! \u2705 caf\u00e9" in raw.getvalue().decode("utf-8")
