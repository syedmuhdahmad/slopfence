"""The engine: how a run is put together, whatever the rules find."""

import ast
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import slopfence
from slopfence.engine import run
from tests.conftest import FakeRegistry

_DOCUMENTED = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def test_run_has_one_docstring_for_both_options():
    """#55: ``run`` documents ``check_imports`` and ``private_index`` in one docstring."""
    doc = inspect.getdoc(run)
    assert "check_imports" in doc and "private_index" in doc


def test_no_second_string_after_a_docstring():
    """#55: a string literal right after a docstring is a statement that does nothing."""
    doubled = []
    for path in sorted(Path(slopfence.__file__).parent.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, _DOCUMENTED) or len(node.body) < 2:
                continue
            second = node.body[1]
            if (
                ast.get_docstring(node, clean=False) is not None
                and isinstance(second, ast.Expr)
                and isinstance(second.value, ast.Constant)
                and isinstance(second.value.value, str)
            ):
                doubled.append(f"{path.name}:{second.lineno}")
    assert doubled == []


def test_findings_at_one_position_are_ordered_by_message(write, check):
    """``import a, b`` gives one finding per name, all at the same line and column.
    The message breaks the tie, so the report doesn't change with the hash seed."""
    names = [f"ghost_{letter}_zz" for letter in "hbfjaedicg"]
    write("app.py", f"import {', '.join(names)}\n")
    result = check("SLOP001", registry=FakeRegistry(set()), check_imports=True)
    assert {(f.line, f.col) for f in result.findings} == {(1, 1)}
    assert [f.message.split("'")[1] for f in result.findings] == sorted(names)


def test_report_is_the_same_for_every_hash_seed(tmp_path):
    """Same input, same report, in the same order: also in a new process, where strings
    hash differently. Four missing dependencies on one line are four findings at one
    position, and the order of a set of them would change from process to process."""
    names = ["zz-fake-one", "aa-fake-two", "mm-fake-three", "kk-fake-four"]
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "demo"\nversion = "1"\ndependencies = {json.dumps(names)}\n',
        encoding="utf-8",
    )
    cache = tmp_path / "cache" / "slopfence"
    cache.mkdir(parents=True)
    (cache / "pypi.json").write_text(
        json.dumps({name: [False, 9e12] for name in names}), encoding="utf-8"
    )
    # The package under test, however this test run found it. It goes in front of what
    # is there already: on Python 3.10 that may be where tomli comes from.
    package = str(Path(slopfence.__file__).parents[1])
    search_path = os.pathsep.join(filter(None, [package, os.environ.get("PYTHONPATH")]))
    reports = set()
    for seed in ("0", "1", "2"):
        env = dict(
            os.environ,
            PYTHONHASHSEED=seed,
            SLOPFENCE_CACHE_DIR=str(tmp_path / "cache"),
            PYTHONPATH=search_path,
        )
        done = subprocess.run(
            # The machine's own pip settings must not decide whether names are looked up.
            [
                sys.executable,
                "-m",
                "slopfence",
                ".",
                "--format",
                "json",
                "--no-detect-private-index",
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            encoding="utf-8",
        )
        # A process that crashed also ends with status 1. Only a report counts.
        assert done.returncode == 1 and done.stdout.startswith("{"), done.stderr
        reports.add(done.stdout)
    assert len(reports) == 1
    found = [f["message"].split("'")[1] for f in json.loads(reports.pop())["findings"]]
    assert found == sorted(names)
