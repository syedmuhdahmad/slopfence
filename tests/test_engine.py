"""The engine: how a run is put together, whatever the rules find."""

import ast
import inspect
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
