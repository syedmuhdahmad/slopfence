"""Deeply nested code: long operator, call and attribute chains, and long ``elif`` chains.

Generated code has expressions thousands of levels deep, far past Python's default
recursion limit of 1,000. Detectors must not recurse once per level, and a file the
running Python itself can't parse is skipped with the usual warning.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

from slopfence.cli import main
from slopfence.engine import Result
from tests.conftest import rule_lines

# Application code with one finding per rule that walks the whole tree. The deep block
# comes last in ``build``, so the lines of the findings don't depend on it.
APP = '''\
def validate(token):
    """Validate the token."""
    return True


def build(a):
    try:
        send(a)
    except Exception:
        pass
    {block}
    return parts
'''
# A test that can never fail, around the same deep block.
TEST = """\
def test_build(a):
    try:
        assert a
    except Exception:
        pass
    {block}
"""
FILES = ["gen.py", "gen_copy.py", "test_gen.py"]
# What a scan of the three files reports: (path, line, rule), in report order.
EXPECTED = [
    ("gen.py", 3, "SLOP011"),
    ("gen.py", 9, "SLOP040"),
    ("gen_copy.py", 3, "SLOP011"),
    ("gen_copy.py", 6, "SLOP030"),
    ("gen_copy.py", 9, "SLOP040"),
    ("test_gen.py", 4, "SLOP022"),
]


def plus_chain(terms: int) -> str:
    """``join_parts(a) + "s0" + "s1" + ...``: one AST level per term."""
    return " + ".join(["join_parts(a)"] + [f'"s{i}"' for i in range(terms)])


def call_chain(calls: int) -> str:
    """``builder(a).add(0).add(1)...``: two AST levels per call."""
    return "builder(a)" + "".join(f".add({i})" for i in range(calls))


def attribute_chain(attributes: int) -> str:
    """``a.b.b.b...()``: a call on a name that is one AST level deep per attribute."""
    return "a" + ".b" * attributes + "()"


def elif_chain(branches: int) -> str:
    """An ``if`` with ``branches`` ``elif`` branches; each one nests inside the one before."""
    lines = ["if a == 0:", "        parts = 0"]
    for i in range(1, branches + 1):
        lines += [f"    elif a == {i}:", f"        parts = {i}"]
    return "\n".join(lines)


# Several times deeper than the recursion limit, and still within what Python parses on
# every platform CI runs on.
DEEP = {
    "plus chain": f"parts = {plus_chain(1500)}",
    "call chain": f"parts = {call_chain(700)}",
    # The only shape the test rules' ``_dotted`` follows level by level.
    "attribute chain": f"parts = {attribute_chain(1500)}",
    "elif chain": elif_chain(1500),
}
# 3,000 levels: Python 3.11 can't parse this, and neither can 3.12 or 3.13 on Windows.
# The same versions elsewhere, 3.10 and 3.14 can.
AT_PARSER_LIMIT = {
    "plus chain": f"parts = {plus_chain(3000)}",
    "call chain": f"parts = {call_chain(1500)}",
}


def python_parses(source: str) -> bool:
    """Whether the running Python can parse ``source``: its nesting limit varies by version."""
    try:
        ast.parse(source)
    except (RecursionError, MemoryError):
        return False
    return True


# Debug builds of Python and Windows on ARM64 stop below 1,500 levels. There Python can't
# parse these files, so the detectors have nothing to walk.
needs_1500_levels = pytest.mark.skipif(
    not python_parses(f"x = {plus_chain(1500)}"),
    reason="this Python build can't parse 1,500 nesting levels",
)


def scan(write: Callable[[str, str], Path], check: Callable[..., Result], block: str) -> Result:
    """Check a small project in which two functions and a test contain ``block``."""
    write("gen.py", APP.format(block=block))
    write("gen_copy.py", APP.format(block=block))
    write("test_gen.py", TEST.format(block=block))
    return check()


def found(result: Result) -> list[tuple[str, int, str]]:
    """The findings as (path, line, rule), in report order."""
    return [(f.path, f.line, f.rule) for f in result.findings]


@needs_1500_levels
def test_file_from_the_bug_report_is_checked(tmp_path, capsys):
    """A 1,500-term ``+`` chain ended in a RecursionError traceback and exit code 1."""
    code = f"def build(a):\n    parts = {plus_chain(1500)}\n\n    return parts\n"
    (tmp_path / "gen.py").write_text(code, encoding="utf-8")
    assert main([str(tmp_path), "--offline", "--no-color"]) == 0
    assert capsys.readouterr().out.strip() == "No issues found in 1 files."


@needs_1500_levels
@pytest.mark.parametrize("block", DEEP.values(), ids=DEEP)
def test_findings_next_to_deep_nesting_are_reported(write, check, block):
    """Every rule that walks the tree gets through deep nesting and reports as usual."""
    result = scan(write, check, block)
    assert result.parse_errors == []
    assert found(result) == EXPECTED


@pytest.mark.parametrize("block", AT_PARSER_LIMIT.values(), ids=AT_PARSER_LIMIT)
def test_nesting_at_the_parser_limit_is_checked_or_skipped(write, check, block):
    """Checked where Python can parse the file, reported as unparseable where it can't."""
    parsed = python_parses(APP.format(block=block))
    result = scan(write, check, block)
    assert result.parse_errors == ([] if parsed else FILES)
    assert found(result) == (EXPECTED if parsed else [])


def test_file_too_deep_for_python_is_skipped_and_the_rest_is_checked(write, check):
    """No supported Python parses 7,000 ``elif`` branches: its parser raises MemoryError."""
    write("dispatch.py", f"def dispatch(a):\n    {elif_chain(7000)}\n    return parts\n")
    write("app.py", 'def validate(token):\n    """Validate the token."""\n    return True\n')
    result = check()
    assert result.parse_errors == ["dispatch.py"]
    assert found(result) == [("app.py", 3, "SLOP011")]


@pytest.mark.parametrize("refusal", [RecursionError, MemoryError])
def test_file_the_parser_refuses_is_skipped(write, check, monkeypatch, refusal):
    """Whichever way the parser gives up on a file, the file is skipped.

    The refusal is simulated: no input makes every supported Python raise
    RecursionError (3.10 has no limit, and the limit of 3.14 depends on the stack size).
    """

    def refuse(*args: object, **kwargs: object) -> ast.Module:
        """Stand in for a parser that gives up on its input."""
        raise refusal

    write("app.py", "x = 1\n")
    with monkeypatch.context() as patch:
        patch.setattr(ast, "parse", refuse)
        result = check()
    assert result.parse_errors == ["app.py"]
    assert result.findings == []


def test_file_too_deep_for_python_gets_the_usual_warning(tmp_path, capsys):
    """Like a syntax error: a warning and exit code 0, or exit code 2 with --strict."""
    code = f"def dispatch(a):\n    {elif_chain(7000)}\n    return parts\n"
    (tmp_path / "dispatch.py").write_text(code, encoding="utf-8")
    assert main([str(tmp_path), "--offline", "--no-color"]) == 0
    assert "warning: could not parse dispatch.py" in capsys.readouterr().out
    assert main([str(tmp_path), "--offline", "--no-color", "--strict"]) == 2


@needs_1500_levels
@pytest.mark.parametrize(
    ("condition", "lines"),
    [
        pytest.param(plus_chain(1500), [4], id="without"),
        # The first term is the deepest node of the chain.
        pytest.param(f"TYPE_CHECKING + {plus_chain(1500)}", [], id="with"),
    ],
)
def test_long_if_condition_is_searched_for_type_checking(write, check, condition, lines):
    """SLOP011 skips ``if TYPE_CHECKING`` blocks, however long the condition is."""
    stub = '    def validate(token):\n        """Validate the token."""\n        return True\n'
    write("app.py", f"if {condition}:\n{stub}")
    result = check("SLOP011")
    assert result.parse_errors == []
    assert rule_lines(result, "SLOP011") == lines


@needs_1500_levels
@pytest.mark.parametrize(("last_type", "lines"), [("T1500", [3]), ("None", [])])
def test_long_return_annotation_is_read(write, check, last_type, lines):
    """SLOP011 reads a union of 1,500 types to see whether ``None`` satisfies it."""
    union = " | ".join([f"T{i}" for i in range(1500)] + [last_type])
    write("app.py", f'def fetch_user(user_id) -> {union}:\n    """Fetch the user."""\n    pass\n')
    result = check("SLOP011")
    assert result.parse_errors == []
    assert rule_lines(result, "SLOP011") == lines


def test_deepest_literal_python_allows_is_read(write, check):
    """Python refuses more than 200 nested brackets, so a literal can't nest deeper."""
    nested = "[" * 199 + "1" + "]" * 199
    write("app.py", f'def fetch_matrix(size):\n    """Fetch the matrix."""\n    return {nested}\n')
    result = check("SLOP011")
    assert result.parse_errors == []
    assert rule_lines(result, "SLOP011") == [3]
