import json
import os
import stat
from unittest import mock

import pytest

from slopfence import cli as climod
from slopfence import diff as diffmod
from slopfence import files as filesmod
from slopfence.cli import main
from tests.conftest import git as _git
from tests.conftest import git_init

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
    """With ``--diff``, findings on lines the branch did not touch are left out."""
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


@pytest.mark.parametrize(
    "setting",
    ["diff.mnemonicPrefix=true", "diff.noprefix=true", "diff.srcPrefix=x/", "diff.dstPrefix=y/"],
)
def test_diff_mode_does_not_depend_on_the_git_prefix_settings(tmp_path, setting):
    """A user's git config can change the ``a/`` and ``b/`` in front of the paths of a
    diff. The changed lines must be found all the same (#65), also in a folder named
    ``b``, whose path starts like a prefix."""
    git_init(tmp_path)
    (tmp_path / "b").mkdir()
    for name in ("app.py", "b/app.py"):
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    for name in ("app.py", "b/app.py"):
        (tmp_path / name).write_text("x = 1\n# I hope this helps!\n", encoding="utf-8")
    _git(tmp_path, "config", *setting.split("="))
    assert diffmod.changed_lines(tmp_path, "HEAD") == {"app.py": {2}, "b/app.py": {2}}


def test_diff_with_bad_ref(tmp_path, monkeypatch):
    """A ref that git does not know is an error (exit code 2), not an empty diff."""
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
    """Virtual environments, node_modules and egg-info folders are never walked."""
    from slopfence.engine import discover_all

    (tmp_path / "app.py").write_text("")
    for d in [".venv/lib", "node_modules/x", "pkg.egg-info"]:
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "m.py").write_text("")
    py_files, _, _ = discover_all([tmp_path], tmp_path)
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


def _survive_the_sigint_re_raise(monkeypatch):
    """The real handler raises SIGINT at this process; tests must stay alive."""
    monkeypatch.setattr(climod, "_end_like_an_interrupted_program", lambda: None)


def test_ctrl_c_while_the_config_is_read_exits_130(tmp_path, capsys, monkeypatch):
    """A Ctrl-C while the config is read ends like an interrupted program (#68)."""
    (tmp_path / "clean.py").write_text("x = 1\n")
    _survive_the_sigint_re_raise(monkeypatch)
    monkeypatch.setattr(climod, "load_config", mock.Mock(side_effect=KeyboardInterrupt))
    assert main([str(tmp_path), "--offline"]) == climod.EXIT_INTERRUPTED
    assert "slopfence: interrupted" in capsys.readouterr().err


def test_ctrl_c_while_the_diff_runs_exits_130(tmp_path, capsys, monkeypatch):
    """A Ctrl-C while `git diff` runs for --diff ends the run the same way (#68)."""
    (tmp_path / "clean.py").write_text("x = 1\n")
    _survive_the_sigint_re_raise(monkeypatch)
    monkeypatch.setattr(climod.diffmod, "changed_lines", mock.Mock(side_effect=KeyboardInterrupt))
    assert main([str(tmp_path), "--offline", "--diff", "HEAD"]) == climod.EXIT_INTERRUPTED
    assert "slopfence: interrupted" in capsys.readouterr().err


def test_ctrl_c_during_the_scan_still_exits_130(tmp_path, capsys, monkeypatch):
    """A Ctrl-C during the scan still prints the plain message and exits 130 (#68)."""
    (tmp_path / "clean.py").write_text("x = 1\n")
    _survive_the_sigint_re_raise(monkeypatch)
    monkeypatch.setattr(climod, "run", mock.Mock(side_effect=KeyboardInterrupt))
    assert main([str(tmp_path), "--offline"]) == climod.EXIT_INTERRUPTED
    assert "slopfence: interrupted" in capsys.readouterr().err


def test_ctrl_c_while_the_report_is_written_exits_130_and_keeps_the_old_file(
    tmp_path, capsys, monkeypatch
):
    """An interrupt during the report write leaves the previous report file as it was (#68)."""
    (tmp_path / "clean.py").write_text("x = 1\n")
    _survive_the_sigint_re_raise(monkeypatch)
    report = tmp_path / "report.json"
    report.write_text('{"old": true}')
    # The rename is the last step of the one-step write, so interrupting it is the
    # moment the old file has to survive.
    monkeypatch.setattr(filesmod.os, "replace", mock.Mock(side_effect=KeyboardInterrupt))
    assert main([str(tmp_path), "--offline", "--format", "json", "-o", str(report)]) == (
        climod.EXIT_INTERRUPTED
    )
    assert report.read_text() == '{"old": true}'
    assert not list(tmp_path.glob("*.tmp")), "the temporary file is cleaned up"
    assert "slopfence: interrupted" in capsys.readouterr().err


def test_report_to_a_device_is_written_in_place(tmp_path):
    """`-o` on a device such as /dev/null writes through it instead of renaming onto it (#68)."""
    (tmp_path / "clean.py").write_text("x = 1\n")
    assert main([str(tmp_path), "--offline", "-o", os.devnull]) == climod.EXIT_OK
    if os.name != "nt":
        assert stat.S_ISCHR(os.stat(os.devnull).st_mode), "/dev/null is still the null device"


@pytest.mark.skipif(os.name == "nt", reason="making a symlink needs a privilege on Windows")
def test_report_through_a_symlink_updates_the_real_file(tmp_path):
    """`-o` on a symbolic link writes the file it names and leaves the link a link (#68)."""
    (tmp_path / "clean.py").write_text("x = 1\n")
    real = tmp_path / "real.json"
    real.write_text("{}")
    link = tmp_path / "report.json"
    link.symlink_to(real)

    assert main([str(tmp_path), "--offline", "--format", "json", "-o", str(link)]) == climod.EXIT_OK
    assert link.is_symlink(), "the link is not replaced by a plain file"
    assert real.read_text() != "{}", "the file the link names was written"


def test_unwritable_report_target_exits_2(tmp_path, capsys):
    """A report that can't be written is an error (exit 2), not a findings result (#68)."""
    (tmp_path / "clean.py").write_text("x = 1\n")
    target = tmp_path / "a-folder"
    target.mkdir()

    assert main([str(tmp_path), "--offline", "-o", str(target)]) == climod.EXIT_ERROR
    assert "can't write report" in capsys.readouterr().err


# Root may write anywhere, so the permission tests below would prove nothing.
_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.mark.skipif(not os.path.isdir("/dev/fd"), reason="no /dev/fd on this platform")
def test_report_to_a_pipe_is_written_in_place(tmp_path):
    """`-o /dev/stdout | cmd` and `-o >(cmd)` name a pipe through /dev/fd: the report goes in."""
    (tmp_path / "clean.py").write_text("x = 1\n")
    read_end, write_end = os.pipe()
    try:
        args = [str(tmp_path), "--offline", "--format", "json", "-o", f"/dev/fd/{write_end}"]
        assert main(args) == climod.EXIT_OK
    finally:
        os.close(write_end)
    with os.fdopen(read_end, encoding="utf-8") as stream:
        assert "version" in json.loads(stream.read())


@pytest.mark.skipif(os.name == "nt" or _ROOT, reason="needs POSIX folder permissions, not root")
def test_report_in_a_read_only_folder_is_still_written(tmp_path):
    """An existing report in a folder the user can't write to is updated in place, as in 0.3."""
    (tmp_path / "clean.py").write_text("x = 1\n")
    folder = tmp_path / "reports"
    folder.mkdir()
    report = folder / "report.json"
    report.write_text("{}")
    folder.chmod(0o555)
    try:
        args = [str(tmp_path), "--offline", "--format", "json", "-o", str(report)]
        assert main(args) == climod.EXIT_OK
    finally:
        folder.chmod(0o755)  # the temporary folder has to be removable
    assert "version" in json.loads(report.read_text())


@pytest.mark.skipif(_ROOT, reason="root may write to any file")
def test_read_only_report_is_not_replaced(tmp_path, capsys):
    """A report file the user made read-only keeps its content, and the run exits 2."""
    (tmp_path / "clean.py").write_text("x = 1\n")
    report = tmp_path / "report.json"
    report.write_text("{}")
    report.chmod(stat.S_IREAD)
    try:
        assert main([str(tmp_path), "--offline", "-o", str(report)]) == climod.EXIT_ERROR
        assert report.read_text() == "{}"
    finally:
        report.chmod(stat.S_IREAD | stat.S_IWRITE)
    assert "can't write report" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="Windows files have no POSIX permissions")
def test_replaced_report_keeps_its_permissions(tmp_path):
    """A report that only its owner may read stays that way when a run replaces it."""
    (tmp_path / "clean.py").write_text("x = 1\n")
    report = tmp_path / "report.json"
    report.write_text("{}")
    report.chmod(0o600)
    assert main([str(tmp_path), "--offline", "--format", "json", "-o", str(report)]) == 0
    assert "version" in json.loads(report.read_text())
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
