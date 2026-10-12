"""SLOP011 (stub functions) and SLOP040 (silently swallowed exceptions).

Both only check application code: tests have their own rules (SLOP022), and
demo or example code is allowed to cut corners.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator

from slopfence.models import Finding
from slopfence.source import SourceFile, child_statements

# --- SLOP040 ------------------------------------------------------------------------

BROAD_EXCEPTIONS = {"Exception", "BaseException"}
# Functions where ignoring every error is the norm: finalizers and shutdown paths.
CLEANUP_FUNCTIONS = {
    "__del__",
    "__exit__",
    "__aexit__",
    "close",
    "aclose",
    "cleanup",
    "clean_up",
    "shutdown",
    "teardown",
    "tear_down",
    "dispose",
    "release",
    "stop",
    "terminate",
    "kill",
}
# Calls whose failure is usually irrelevant: closing, deleting, killing.
CLEANUP_CALLS = CLEANUP_FUNCTIONS | {
    "remove",
    "unlink",
    "rmtree",
    "rmdir",
    "unregister",
    "disconnect",
    "cancel",
    "join",
    "flush",
    "wait",
}


def _last_name(node: ast.AST) -> str:
    """The last part of a name or attribute: ``os.path.join`` -> ``join``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_broad(handler: ast.ExceptHandler) -> bool:
    """Whether a handler catches everything: bare, ``Exception`` or ``BaseException``."""
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(_last_name(t) in BROAD_EXCEPTIONS for t in types)


def _does_nothing(body: list[ast.stmt]) -> bool:
    """Only ``pass``, ``...`` or a bare string."""
    return all(
        isinstance(stmt, ast.Pass)
        or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
        for stmt in body
    )


def _only_cleanup_calls(body: list[ast.stmt]) -> bool:
    """A try body that only closes, deletes or stops things, maybe in a loop."""
    if len(body) == 1 and isinstance(body[0], ast.For):
        body = body[0].body  # for d in reversed(created): os.rmdir(d)
    calls = [s.value for s in body if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)]
    return len(calls) == len(body) and all(_last_name(c.func) in CLEANUP_CALLS for c in calls)


def _only_imports(body: list[ast.stmt]) -> bool:
    """A try body that only imports: an optional dependency that may be broken."""
    return all(isinstance(stmt, (ast.Import, ast.ImportFrom)) for stmt in body)


def _atexit_functions(src: SourceFile) -> set[str]:
    """Names registered with ``atexit.register(f)`` or decorated ``@atexit.register``."""
    names = set()
    for node in src.nodes:
        if isinstance(node, ast.Call) and _last_name(node.func) == "register" and node.args:
            names.add(_last_name(node.args[0]))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            _last_name(d) == "register" for d in node.decorator_list
        ):
            names.add(node.name)
    return names


def _enclosing_functions(tree: ast.Module) -> dict[int, str]:
    """For every statement (by node id), the name of the innermost function around it."""
    owner: dict[int, str] = {}
    todo: list[tuple[ast.AST, str]] = [(tree, "")]
    while todo:
        node, current = todo.pop()
        for child in child_statements(node):
            name = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else current
            )
            owner[id(child)] = name
            todo.append((child, name))
    return owner


def _has_comment(src: SourceFile, first: int, last: int) -> bool:
    """Whether any comment sits on lines ``first`` to ``last``."""
    return any(first <= c.line <= last for c in src.comments)


def _silent_handlers(src: SourceFile) -> Iterator[tuple[ast.Try, ast.ExceptHandler]]:
    """Every broad handler that does nothing, with its ``try``, in ``ast.walk`` order."""
    for node in src.nodes:
        if (
            not isinstance(node, ast.Try)
            or _only_cleanup_calls(node.body)
            or _only_imports(node.body)
        ):
            continue
        for handler in node.handlers:
            if _is_broad(handler) and _does_nothing(handler.body):
                yield node, handler


def check_swallowed_exceptions(src: SourceFile) -> Iterator[Finding]:
    """SLOP040: ``except Exception: pass`` that hides every error, in application code."""
    if src.is_test_file or src.is_example_file:
        return
    handlers = list(_silent_handlers(src))
    if not handlers:
        return  # most files: the two walks below are not needed
    owners = _enclosing_functions(src.tree)
    atexit_funcs = _atexit_functions(src)
    for node, handler in handlers:
        func = owners.get(id(node), "")
        if func in CLEANUP_FUNCTIONS or func in atexit_funcs:
            continue
        # A comment ("# best effort", "# optional dependency") shows it's deliberate.
        if _has_comment(src, handler.lineno, handler.end_lineno or handler.lineno):
            continue
        caught = "except:" if handler.type is None else "except Exception"
        yield Finding(
            "SLOP040",
            src.rel,
            handler.lineno,
            handler.col_offset + 1,
            f"'{caught}' silently swallows every error; catch a specific exception, "
            "or log or re-raise it",
            end_line=handler.end_lineno,
        )


# --- SLOP011 ------------------------------------------------------------------------

# Docstrings that start with one of these promise work the body doesn't do.
ACTION_VERBS = {
    "analyse",
    "analyze",
    "authenticate",
    "authorize",
    "calculate",
    "charge",
    "check",
    "compute",
    "connect",
    "create",
    "delete",
    "download",
    "encrypt",
    "decrypt",
    "fetch",
    "generate",
    "hash",
    "load",
    "parse",
    "persist",
    "predict",
    "process",
    "query",
    "refund",
    "remove",
    "retrieve",
    "save",
    "send",
    "store",
    "sync",
    "update",
    "upload",
    "validate",
    "verify",
}
# Wording that admits the function isn't done. ("Dummy", "mock" or "hardcoded" usually
# describe deliberate test doubles and constants, so they don't count.)
PLACEHOLDER_DOC = re.compile(
    r"(?i:\bplaceholder\b|\bfor now\b|\bnot implemented yet\b|\bin a real (implementation|app))"
    r"|\b(TODO|FIXME)\b",  # upper case only: "todo list" is prose
)
# Defaults meant to be overridden or deliberately empty: hooks, null objects.
HOOK_DOC = re.compile(
    r"overrid|subclass|\bhook\b|no-?op\b|does nothing|has no effect|not used|"
    r"intended to be implemented|by default",
    re.I,
)
EXEMPT_DECORATORS = {
    "abstractmethod",
    "abstractproperty",
    "overload",
    "property",
    "cached_property",
    "setter",
    "getter",
    "deleter",
    "fixture",
}
EXEMPT_BASES = {"Protocol", "ABC", "ABCMeta", "TypedDict", "NamedTuple", "Enum"}
NONE_ANNOTATIONS = {"None", "NoReturn", "Never", "Any", "object"}


def _first_word(doc: str) -> str:
    """The docstring's first word as a verb's base form (``Fetches`` -> ``fetch``)."""
    words = re.findall(r"[A-Za-z]+", doc)
    word = words[0].lower() if words else ""
    # "Validates the token" -> "validate"; "Fetches" -> "fetch".
    for suffix in ("es", "s"):
        if word.endswith(suffix) and word[: -len(suffix)] in ACTION_VERBS:
            return word[: -len(suffix)]
    return word


def _is_fake_value(node: ast.expr | None) -> bool:
    """A hardcoded result: True, or a literal collection of constants."""
    if isinstance(node, ast.Constant):
        return node.value is True
    if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
        parts = [*node.keys, *node.values] if isinstance(node, ast.Dict) else list(node.elts)
        return bool(parts) and all(
            isinstance(p, ast.Constant) or _is_fake_value(p) for p in parts if p is not None
        )
    return False


def _returns_nothing(body: list[ast.stmt]) -> bool:
    """A body that is only ``pass``, ``...``, ``return`` or ``return None``."""
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass) or (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is Ellipsis
    ):
        return True
    return isinstance(stmt, ast.Return) and (
        stmt.value is None or (isinstance(stmt.value, ast.Constant) and stmt.value.value is None)
    )


def _promises_a_value(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A return annotation that ``None`` doesn't satisfy (not None, Optional or Any)."""
    ann = func.returns
    if ann is None:
        return False
    text = ast.unparse(ann)
    if "None" in text or "Optional" in text:
        return False
    return _last_name(ann) not in NONE_ANNOTATIONS and text not in NONE_ANNOTATIONS


def _ignores_its_inputs(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Takes parameters (besides self/cls) and uses none of them."""
    args = func.args
    params = {
        a.arg
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
        if a
    } - {"self", "cls"}
    used = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
    return bool(params) and not params & used


def _exempt_function(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Dunder methods and decorated interfaces (abstract, overload, property)."""
    if func.name.startswith("__") and func.name.endswith("__"):
        return True
    return any(
        _last_name(d.func if isinstance(d, ast.Call) else d) in EXEMPT_DECORATORS
        for d in func.decorator_list
    )


def _candidate_functions(
    tree: ast.Module,
) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Functions that are meant to be real implementations: not in Protocols, ABCs or
    ``if TYPE_CHECKING`` blocks."""

    # Depth-first, in source order. Functions are only found through statements.
    todo = child_statements(tree)[::-1]
    while todo:
        child = todo.pop()
        if isinstance(child, ast.If) and "TYPE_CHECKING" in ast.unparse(child.test):
            continue
        if isinstance(child, ast.ClassDef):
            bases = {
                _last_name(b.value if isinstance(b, ast.Subscript) else b) for b in child.bases
            }
            metaclass = {_last_name(k.value) for k in child.keywords}
            if bases & EXEMPT_BASES or metaclass & EXEMPT_BASES:
                continue
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield child
        todo.extend(child_statements(child)[::-1])


def check_stub_functions(src: SourceFile) -> Iterator[Finding]:
    """SLOP011: a function whose docstring promises work but whose body is a stub."""
    # .pyi stubs are all stubs by design (discovery only picks .py files anyway).
    if src.is_test_file or src.is_example_file or src.path.suffix == ".pyi":
        return
    for func in _candidate_functions(src.tree):
        doc = ast.get_docstring(func)
        if not doc or _exempt_function(func):
            continue
        body = func.body[1:]  # without the docstring
        if not body:
            continue
        if HOOK_DOC.search(doc):
            continue
        placeholder = bool(PLACEHOLDER_DOC.search(doc))
        promises = _first_word(doc) in ACTION_VERBS
        stmt = body[0]
        # Hardcoded result while ignoring every input: validate_token(token) -> True.
        fake = (
            len(body) == 1
            and isinstance(stmt, ast.Return)
            and _is_fake_value(stmt.value)
            and (_ignores_its_inputs(func) or placeholder)
        )
        # Nothing returned although the signature promises a value.
        empty = _returns_nothing(body) and _promises_a_value(func)
        if not (
            ((fake or empty) and (promises or placeholder))
            or (placeholder and _returns_nothing(body))
        ):
            continue
        what = "returns hardcoded data" if fake else "does nothing"
        summary = doc.strip().splitlines()[0].strip().rstrip(".")[:60]
        yield Finding(
            "SLOP011",
            src.rel,
            stmt.lineno,
            stmt.col_offset + 1,
            f"Function '{func.name}' {what}, but its docstring says {summary!r}",
            end_line=func.end_lineno,
        )
