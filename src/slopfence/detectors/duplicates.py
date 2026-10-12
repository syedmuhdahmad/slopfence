"""SLOP030: near-duplicate functions across the project.

Each function is normalized (docstring dropped, parameters and local variables
renamed by position) and turned into a token sequence. Identical sequences are
duplicates. For near-duplicates, each function gets a small MinHash sketch of its
token 4-grams; functions sharing at least ``MIN_SHARED`` sketch values are compared
with difflib and reported above ``SIMILARITY``. Functions with fewer than ``MIN_STATEMENTS``
statements are ignored: short helpers look alike by nature.

Measured on 122,000 functions from the stdlib and 225 packages, these are not
reported, because they are almost always deliberate:

* methods with the same name in different classes (plugins, drivers, platform
  variants: ``seek`` in two image formats);
* parallel families with the same structure and different values: every
  difference is a same-length substitution (``polyadd`` and ``polysub``,
  ``read_uint2`` and ``read_uint8``), or at most ``MIRROR_TOKENS`` tokens differ
  (``md5_utf8`` and ``sha_utf8``). A copy someone edited has insertions or
  deletions instead;
* functions with the same name in the same file (alternatives under ``if``/``try``);
* near-duplicate methods. Methods are only reported when identical; near-duplicates
  are reported for standalone functions, the helpers AI assistants re-create.

Even then, near-duplicates in established code are mostly deliberate twins
(``nanargmin``/``nanargmax``, ``getinnerframes``/``getouterframes``), so a full scan
reports identical copies only. With ``--diff``, functions you changed are also
checked for near-duplicates of existing code: that's when an assistant re-writes a
helper that already exists.
"""

from __future__ import annotations

import ast
import difflib
import itertools
import zlib
from collections import Counter, defaultdict
from collections.abc import Collection, Iterator
from dataclasses import dataclass

from slopfence.diff import ChangedLines, touches
from slopfence.models import Finding
from slopfence.source import SourceFile, child_statements, children, walk

MIN_STATEMENTS = 3
SIMILARITY = 0.9
# Sketch values shared by more functions than this are boilerplate; skipping them
# keeps the comparison count low on big projects.
MAX_GROUP = 200
SKETCH_SIZE = 12
# Functions 90% alike share most of their sketch; one shared value is often chance.
MIN_SHARED = 3
NGRAM = 4
# Near-duplicates differing in this many tokens or fewer are parallel functions.
MIRROR_TOKENS = 2
# Longer functions are generated code; difflib is quadratic, so they're only checked
# for identical copies.
MAX_NEAR_TOKENS = 5000

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
# The ``ctx`` nodes of names, attributes and subscripts. They are not tokens.
_CONTEXTS = (ast.Load, ast.Store, ast.Del)


# Joins a function's tokens into one string. No token can contain it: names can't,
# and repr() writes it as an escape sequence in constants.
TOKEN_SEP = "\x1f"


@dataclass(frozen=True)
class Function:
    """Where a function is and what it looks like once names and layout are taken away."""

    name: str
    path: str
    line: int
    end_line: int
    # The normalized token sequence, joined with TOKEN_SEP. One string per function
    # is cheap to compare, to hash, to keep for a whole project and to send between
    # processes; a tuple of tokens is none of these.
    fingerprint: str
    # The class a method is defined in (None for functions).
    owner: str | None = None

    @property
    def tokens(self) -> list[str]:
        """The token sequence (only near-duplicates need it)."""
        return self.fingerprint.split(TOKEN_SEP)

    @property
    def size(self) -> int:
        """The number of tokens."""
        return self.fingerprint.count(TOKEN_SEP) + 1


def _statement_count(func: FunctionNode) -> int:
    """Statements inside the function, nested ones included."""
    count = 0
    todo = child_statements(func)
    while todo:
        node = todo.pop()
        # ``except`` handlers and ``match`` cases hold statements but aren't statements.
        count += isinstance(node, ast.stmt)
        todo.extend(child_statements(node))
    return count


def _local_names(func: FunctionNode) -> set[str]:
    """Parameters and every name the function assigns or deletes."""
    args = func.args
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    names = {a.arg for a in params if a}
    for node in walk(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
    return names


def _body_without_docstring(func: FunctionNode) -> list[ast.stmt]:
    """The function body without its docstring, if it has one."""
    body = func.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def _tokens(node: ast.AST, rename: dict[str, str], local: set[str], out: list[str]) -> None:
    """Append a flat token sequence of node types and the values that matter to ``out``.

    Depth-first, so each statement's tokens stay together and an edit changes one
    stretch only. Parameters and locals become v0, v1... in order of appearance,
    so renaming them doesn't change the sequence.
    """
    # A stack instead of recursion: an expression can be thousands of levels deep.
    todo = [node]
    while todo:
        node = todo.pop()
        if isinstance(node, _CONTEXTS):
            continue
        out.append(type(node).__name__)
        if isinstance(node, (ast.Name, ast.arg)):
            name = node.id if isinstance(node, ast.Name) else node.arg
            out.append(rename.setdefault(name, f"v{len(rename)}") if name in local else name)
        elif isinstance(node, ast.Attribute):
            out.append(node.attr)
        elif isinstance(node, ast.Constant):
            # The full value: constants that differ anywhere (long SQL, URLs) must never
            # make two functions look identical.
            out.append(repr(node.value))
        # Reversed, so the first child is taken off the stack first.
        todo.extend(reversed(children(node)))


def _functions(
    node: ast.AST, owner: str | None = None
) -> Iterator[tuple[FunctionNode, str | None]]:
    """Every function and method under ``node``, with the class it's defined in."""
    for child in child_statements(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield child, owner
            yield from _functions(child, None)
        elif isinstance(child, ast.ClassDef):
            yield from _functions(child, child.name)
        else:
            yield from _functions(child, owner)


def index_functions(src: SourceFile) -> list[Function]:
    """Normalized fingerprints of every non-trivial function in a file (none for test code)."""
    found: list[Function] = []
    if src.is_test_file:
        return found
    for func, owner in _functions(src.tree):
        if _statement_count(func) < MIN_STATEMENTS:
            continue
        local, rename, tokens = _local_names(func), {}, []
        _tokens(func.args, rename, local, tokens)
        for stmt in _body_without_docstring(func):
            _tokens(stmt, rename, local, tokens)
        found.append(
            Function(
                name=func.name,
                path=src.rel,
                line=func.lineno,
                end_line=func.end_lineno or func.lineno,
                fingerprint=TOKEN_SEP.join(tokens),
                owner=owner,
            )
        )
    return found


def _similarity(a: Function, b: Function) -> float:
    """Similarity of two token sequences (1.0 means identical; 0.0 for mirror pairs)."""
    if a.fingerprint == b.fingerprint:
        return 1.0
    matcher = difflib.SequenceMatcher(None, a.tokens, b.tokens, autojunk=False)
    if matcher.real_quick_ratio() < SIMILARITY or matcher.quick_ratio() < SIMILARITY:
        return 0.0
    ratio = matcher.ratio()
    if ratio >= SIMILARITY:
        edits = [op for op in matcher.get_opcodes() if op[0] != "equal"]
        changed = sum(max(i2 - i1, j2 - j1) for _, i1, i2, j1, j2 in edits)
        substitutions_only = all(
            tag == "replace" and i2 - i1 == j2 - j1 for tag, i1, i2, j1, j2 in edits
        )
        if changed <= MIRROR_TOKENS or substitutions_only:
            return 0.0  # a deliberate parallel function, not an edited copy
    return ratio


def _alternatives(a: Function, b: Function) -> bool:
    """Same name and deliberately alike: methods of one interface in different classes
    (plugins, drivers), or alternative definitions in one file (under ``if``/``try``)."""
    if a.name != b.name:
        return False
    if a.path == b.path and a.owner == b.owner:
        return True
    return bool(a.owner and b.owner)


def _sketch(tokens: list[str]) -> list[int]:
    """Bottom-k MinHash of the token 4-grams, with a hash that's stable across runs."""
    grams = {
        zlib.crc32(TOKEN_SEP.join(tokens[i : i + NGRAM]).encode())
        for i in range(max(1, len(tokens) - NGRAM + 1))
    }
    return sorted(grams)[:SKETCH_SIZE]


def _nested(a: Function, b: Function) -> bool:
    """Whether one function is defined inside the other."""
    if a.path != b.path:
        return False
    return (a.line <= b.line and b.end_line <= a.end_line) or (
        b.line <= a.line and a.end_line <= b.end_line
    )


def find_duplicates(
    functions: list[Function], near_for: set[int] | None = None
) -> dict[int, list[tuple[int, float]]]:
    """For each function (by index), the other functions it duplicates, with similarity.

    Identical copies are always found. Near-duplicates are only looked for in pairs
    with a function in ``near_for`` (all functions if None).
    """
    matches: dict[int, list[tuple[int, float]]] = defaultdict(list)
    buckets: dict[int, list[int]] = defaultdict(list)
    for i, func in enumerate(functions if near_for is None or near_for else ()):
        if func.owner is None and func.size <= MAX_NEAR_TOKENS:
            for value in _sketch(func.tokens):
                buckets[value].append(i)
    shared: Counter[tuple[int, int]] = Counter()
    for members in buckets.values():
        if 1 < len(members) <= MAX_GROUP:
            shared.update((i, j) for x, i in enumerate(members) for j in members[x + 1 :])
    candidates = sorted(
        (i, j)
        for (i, j), count in shared.items()
        if count >= MIN_SHARED and (near_for is None or i in near_for or j in near_for)
    )
    for i, j in candidates:
        a_func, b_func = functions[i], functions[j]
        if _nested(a_func, b_func) or _alternatives(a_func, b_func):
            continue  # nested: the outer function contains the inner one's tokens
        if a_func.owner or b_func.owner:
            continue  # methods: only identical copies are reported (below)
        a, b = a_func.size, b_func.size
        if 2 * min(a, b) / (a + b) < SIMILARITY:
            continue  # sizes too different to reach the threshold
        score = _similarity(a_func, b_func)
        if score >= SIMILARITY:
            matches[i].append((j, score))
            matches[j].append((i, score))
    # Exact duplicates, also inside boilerplate buckets. Linking each
    # copy to the first two is enough to report it, and stays linear in the group size.
    exact: dict[str, list[int]] = defaultdict(list)
    for i, func in enumerate(functions):
        exact[func.fingerprint].append(i)
    for members in exact.values():
        for i in members:
            others = (
                m
                for m in members
                if m != i
                and not _nested(functions[i], functions[m])
                and not _alternatives(functions[i], functions[m])
            )
            for j in itertools.islice(others, 2):
                if all(other != j for other, _ in matches[i]):
                    matches[i].append((j, 1.0))
    return matches


def check_duplicates(
    functions: list[Function],
    checked: set[str],
    changed: ChangedLines | None = None,
    *,
    ignored: Collection[str] = (),
    report_unchanged: bool = False,
) -> Iterator[Finding]:
    """SLOP030 for functions in ``checked`` files, compared with all ``functions``.

    ``functions`` are those of the whole project (see ``index_functions``), in any
    order.

    In a full scan, identical copies are reported, each except the first (by path
    and line) pointing at the first. With ``changed``, every changed function that is
    identical or near-identical to another one is reported, pointing at an unchanged
    copy where possible, so a new copy is caught even when it sorts first.

    ``ignored`` are the files where SLOP030 is switched off (``per-file-ignores``).
    They come before every other file. So a copy of their code somewhere else is
    never "the first copy", and it is always reported.

    A baseline is written from a full scan, so it has an entry for every identical
    copy except the first. Three things make a run with ``changed`` agree with it:

    * A changed function that is the first copy names a ``peer``: an identical copy,
      an unchanged one where possible. That copy has the entry.
    * So does a changed function that has no identical copy left, only near-copies,
      when it was the first of them before the diff: it sorts before each of them,
      and some of its lines did not change. A small edit to the first copy of an
      accepted pair leaves exactly that. A function the diff added names no peer.
    * With ``report_unchanged``, the unchanged functions that a full scan reports are
      reported here too. They use up their entries before a changed function with
      the same name can. The caller's ``--diff`` filter drops them afterwards.
    """

    def position(func: Function) -> tuple[bool, str, int]:
        """Sort key: files where SLOP030 is switched off first, then by path and line."""
        return func.path not in ignored, func.path, func.line

    # One order decides everything: which copies find_duplicates links (a function
    # with many identical copies is only linked to the first two), which copy is "the
    # first" and which one a message points at. The caller's order can't be used: it
    # changes with the paths that are checked.
    functions = sorted(functions, key=position)

    def order(k: int) -> tuple[bool, str, int]:
        """Sort key: where the function is."""
        return position(functions[k])

    def is_changed(k: int) -> bool:
        """Whether the function overlaps lines changed in the diff."""
        f = functions[k]
        return changed is not None and touches(changed, f.path, f.line, f.end_line)

    def was_there(k: int) -> bool:
        """Whether a changed function is older than the diff: one of its lines did not change."""
        f = functions[k]
        lines = (changed or {}).get(f.path)  # None: the whole file is new
        return lines is not None and any(n not in lines for n in range(f.line, f.end_line + 1))

    # Near-duplicates are only reported for changed functions (see the module docstring).
    near_for = {k for k in range(len(functions)) if is_changed(k)}
    for i, others in find_duplicates(functions, near_for).items():
        func = functions[i]
        if func.path not in checked or not others:
            continue
        identical = sorted((j for j, score in others if score >= 1.0), key=order)
        # The first copy: a full scan does not report it, the other copies point here.
        is_first = bool(identical) and order(i) < order(identical[0])
        peer = None
        if is_changed(i):
            j, score = min(others, key=lambda m: (is_changed(m[0]), order(m[0])))
            if is_first:
                twin = functions[min(identical, key=lambda k: (is_changed(k), order(k)))]
                peer = (twin.path, twin.name)
            elif not identical and was_there(i) and all(order(i) < order(k) for k, _ in others):
                peer = (functions[j].path, functions[j].name)
        elif identical and not is_first and (changed is None or report_unchanged):
            j, score = identical[0], 1.0
        else:
            continue
        other = functions[j]
        how = "identical to" if score >= 1.0 else f"{int(score * 100)}% similar to"
        yield Finding(
            "SLOP030",
            func.path,
            func.line,
            1,
            f"Function '{func.name}' is {how} '{other.name}' in {other.path}:{other.line}; "
            "reuse it instead of keeping two copies",
            end_line=func.end_line,
            subject=func.name,
            peer=peer,
        )
