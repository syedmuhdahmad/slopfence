"""Baseline mode (#51): accept today's findings, report only new ones."""

import ast
import json
from pathlib import Path

import pytest

import slopfence
from slopfence.cli import main
from slopfence.config import load_config
from slopfence.engine import run
from slopfence.models import RULES
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
