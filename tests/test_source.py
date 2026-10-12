"""How a file is read: the shared walk of its syntax tree, and its comments."""

import ast
import tokenize
import warnings
from pathlib import Path

import pytest

import slopfence
from slopfence.source import char_column, child_statements, children, load, parse, walk
from tests.conftest import needs_parser_depth_limit

# Every place a statement can sit: bodies, ``else`` and ``finally`` blocks, ``except``
# handlers and ``match`` cases. Lambdas and comprehensions hold expressions only.
EVERY_BLOCK = '''
import os, sys
from pkg.sub import name as alias

@decorator(option=1)
class Shape(Base, metaclass=Meta):
    """A class."""
    limit: int = 3

    async def area(self, *sides, scale=1.0, **options):
        async with lock() as held, other():
            async for side in sides:
                await held.wait()
            else:
                return None
        return [s * scale for s in sides if s] or {k: v for k, v in options.items()}


def pick(value, default=lambda: (yield)):
    try:
        with open(value) as handle:
            text = handle.read()
    except (OSError, ValueError) as err:
        raise RuntimeError(f"bad {value!r}") from err
    except Exception:
        pass
    else:
        del handle
    finally:
        global seen
        seen = text[1:-1]
    while text:
        if text.startswith("a"):
            break
        elif text := text[1:]:
            continue
        else:
            assert text, "empty"
    for index, char in enumerate(text):
        nonlocal_ = (index, char)
    else:
        nonlocal_ = None
    match value:
        case [first, *rest] if first:
            return first
        case {"key": found} | Shape(limit=found):
            return found
        case _:
            return default()
'''


def trees():
    """The sample above and slopfence's own modules, parsed."""
    package = Path(slopfence.__file__).parent
    sources = [EVERY_BLOCK] + [p.read_text(encoding="utf-8") for p in sorted(package.rglob("*.py"))]
    return [ast.parse(text) for text in sources]


def test_walk_gives_the_nodes_in_ast_walk_order():
    """Rules report findings in the order they meet nodes, so the order must not change."""
    for tree in trees():
        # strict: a missing or an extra node fails the comparison too.
        assert all(a is b for a, b in zip(walk(tree), ast.walk(tree), strict=True))


def test_children_filters_by_kind_and_keeps_the_order():
    """``children`` gives what ``ast.iter_child_nodes`` gives, or only some kinds of it."""
    for tree in trees():
        for node in ast.walk(tree):
            everything = list(ast.iter_child_nodes(node))
            assert children(node) == everything
            assert children(node, ast.expr) == [c for c in everything if isinstance(c, ast.expr)]


def test_child_statements_reach_every_statement():
    """Following statements, handlers and cases finds each statement of a tree once."""
    for tree in trees():
        reached, todo = [], child_statements(tree)
        while todo:
            node = todo.pop()
            reached.append(node)
            todo.extend(child_statements(node))
        statements = [n for n in ast.walk(tree) if isinstance(n, ast.stmt)]
        assert {id(n) for n in statements} <= {id(n) for n in reached}
        assert len(reached) == len({id(n) for n in reached})
        # Nothing else is visited: only the holders of statements, never an expression.
        assert all(isinstance(n, (ast.stmt, ast.excepthandler, ast.match_case)) for n in reached)


def test_child_statements_are_in_source_order():
    """The blocks of a statement come back in the order they are written."""
    tree = ast.parse(EVERY_BLOCK)
    pick = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
    try_stmt = pick.body[0]
    kinds = [type(n).__name__ for n in child_statements(try_stmt)]
    assert kinds == ["With", "ExceptHandler", "ExceptHandler", "Delete", "Global", "Assign"]
    assert [type(n).__name__ for n in child_statements(pick.body[3])] == ["match_case"] * 3
    assert child_statements(pick.args) == []  # the lambda default holds no statement


def test_nodes_are_walked_once_and_shared(write, tmp_path):
    """``SourceFile.nodes`` is the whole tree, built on first use and then kept."""
    src = load(write("app.py", EVERY_BLOCK), tmp_path)
    assert src.nodes is src.nodes
    assert all(a is b for a, b in zip(src.nodes, ast.walk(src.tree), strict=True))
    assert [span.text.strip() for span in src.docstring_lines] == ['"""A class."""']


@pytest.mark.parametrize(
    "expression",
    ["~" * 20_000 + "1", "lambda: " * 5000 + "1", "(" * 5000 + "1" + ")" * 5000],
    ids=["20,000 unary operators", "5,000 lambdas", "5,000 brackets"],
)
def test_text_that_is_too_deep_for_the_parser_is_not_valid_python(write, tmp_path, expression):
    """Python's parser gives up on an expression that is nested thousands deep, with
    a RecursionError, a MemoryError or a SyntaxError, depending on the version.
    ``parse`` and ``load`` return None for each, like for any text that is not Python."""
    text = f"x = {expression}\n"
    assert parse(text, Path("deep.py"), "deep.py") is None
    assert load(write("deep.py", text), tmp_path) is None


@needs_parser_depth_limit
def test_a_sum_of_100_000_numbers_is_read_or_refused_but_never_raises(write, tmp_path):
    """Whether Python's parser takes a sum this long depends on its version: some give
    up with a RecursionError or a MemoryError, newer ones return a tree 100,000 levels
    deep. ``load`` returns None for the first and a file that can be walked for the
    second. It never raises."""
    src = load(write("deep.py", "x = " + " + ".join(["1"] * 100_000) + "\n"), tmp_path)
    assert src is None or sum(isinstance(node, ast.BinOp) for node in src.nodes) == 99_999


def test_a_text_the_tokenizer_turns_down_keeps_its_tree():
    """A carriage return before a character that is not ASCII: Python's parser takes
    the text, and since Python 3.12 its tokenizer fails on it. ``parse`` raises
    nothing. The text keeps its tree, and the comments that were read before."""
    src = parse("x = 1  # first\n\r\u00e9tape = 2\n", Path("<cell>"), "<cell>")
    assert src is not None and len(src.tree.body) == 2
    assert [comment.text for comment in src.comments] == ["# first"]


def test_a_file_without_comments_is_not_tokenized(write, tmp_path, monkeypatch):
    """No ``#`` anywhere means no comment and no ignore directive: the tokenizer is skipped."""

    def no_tokenizing(readline):
        """Fail the test: this file has nothing to tokenize for."""
        raise AssertionError("the file was tokenized")

    plain = write("plain.py", "x = 1\n")
    hash_in_string = write("text.py", 'x = "# slopfence: ignore"\n')
    commented = write("commented.py", "x = 1  # slopfence: ignore[SLOP010]\n# note\n")
    with monkeypatch.context() as patched:
        patched.setattr(tokenize, "generate_tokens", no_tokenizing)
        src = load(plain, tmp_path)
        assert (src.comments, src.ignores, src.ignore_file) == ([], {}, False)
        with pytest.raises(AssertionError, match="tokenized"):
            load(hash_in_string, tmp_path)
    # A ``#`` inside a string is not a comment; a real one still is.
    src = load(hash_in_string, tmp_path)
    assert (src.comments, src.ignores) == ([], {})
    src = load(commented, tmp_path)
    assert [c.text for c in src.comments] == ["# slopfence: ignore[SLOP010]", "# note"]
    assert src.ignores == {1: {"SLOP010"}}


@pytest.mark.parametrize("action", ["error", "always"])
def test_warning_filters_do_not_decide_what_parses(write, tmp_path, action):
    """Python warns about an invalid escape sequence, but the file is valid Python. It
    parses, and quietly, also in a program that turned warnings into errors."""
    path = write("escapes.py", 'PATTERN = "\\d+"\n')
    with warnings.catch_warnings(record=True) as shown:
        warnings.simplefilter(action)
        src = load(path, tmp_path)
    assert src is not None and src.tree.body
    assert shown == []


def test_char_column_counts_characters_where_ast_counts_bytes():
    """``ast`` gives a column as a UTF-8 byte offset. Text before it that is not ASCII
    takes more bytes than characters."""
    line = 'x = "caf\u00e9 \U0001f600"; import os'
    node = ast.parse(line).body[1]
    assert (
        node.col_offset == line.index("import") + 4
    )  # one byte more for \u00e9, three for the emoji
    assert char_column(line, node.col_offset) == line.index("import")
    assert char_column("plain", 3) == 3 and char_column("", 0) == 0
