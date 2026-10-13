"""SLOP030: near-duplicate functions (#11)."""

from functools import partial

from slopfence import diff as diffmod
from slopfence.detectors.duplicates import TOKEN_SEP, check_duplicates, index_functions
from slopfence.engine import run
from slopfence.registry import OfflineRegistry
from slopfence.source import load
from tests.conftest import git, git_init, rule_lines

FORMAT_DATE = '''
def format_date(value, fmt="%Y-%m-%d"):
    """Format a date for display."""
    if value is None:
        return ""
    text = value.strftime(fmt)
    return text.strip()
'''

# Same logic, different names, docstring and style: the classic AI re-implementation.
FORMAT_DATE_COPY = """
def formatDate(d, pattern="%Y-%m-%d"):
    if d is None:
        return ""
    result = d.strftime(pattern)
    return result.strip()
"""

BUILD_QUERY = """
def build_query(table, filters, limit=100):
    clauses = []
    for key, value in filters.items():
        clauses.append(f"{key} = %s")
    sql = f"SELECT * FROM {table}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += f" LIMIT {limit}"
    return sql, list(filters.values())
"""

# The same function with other names, a different default and a few small edits.
BUILD_QUERY_NEAR = """
def make_select(table_name, where, max_rows=50):
    parts = []
    for column, val in where.items():
        parts.append(f"{column} = ?")
    query = f"SELECT * FROM {table_name}"
    if parts:
        query += " WHERE " + " AND ".join(sorted(parts))
    query += f" LIMIT {max_rows}"
    return query, list(where.values())
"""


def messages(result):
    """The SLOP030 messages in a result."""
    return [f.message for f in result.findings if f.rule == "SLOP030"]


def test_renamed_copy_is_identical(write, check):
    """Renamed parameters and locals, and a missing docstring, still match exactly."""
    write("src/helpers.py", FORMAT_DATE)
    write("src/views.py", "import os\n" + FORMAT_DATE_COPY)
    result = check("SLOP030")
    assert messages(result) == [
        "Function 'formatDate' is identical to 'format_date' in src/helpers.py:1; "
        "reuse it instead of keeping two copies"
    ]
    assert rule_lines(result, "SLOP030") == [3]


def git_project(tmp_path):
    """A git repo in tmp_path; returns a helper that runs git in it."""

    git_init(tmp_path)
    return partial(git, tmp_path)


def test_near_duplicate_reported_in_diff_mode(write, tmp_path):
    """A new function that's a tweaked copy of an existing one is reported with --diff."""
    git = git_project(tmp_path)
    write("src/db.py", BUILD_QUERY)
    git("add", "-A")
    git("commit", "-qm", "base")
    write("src/repo.py", BUILD_QUERY_NEAR)
    changed = diffmod.changed_lines(tmp_path, "main")
    result = run([tmp_path], tmp_path, OfflineRegistry(), select=["SLOP030"], changed=changed)
    (msg,) = messages(result)
    assert msg.startswith("Function 'make_select' is 9")
    assert "% similar to 'build_query' in src/db.py:1" in msg


def test_full_scan_reports_identical_copies_only(write, check):
    """Near-duplicates in existing code are mostly deliberate twins: not in a full scan."""
    write("src/db.py", BUILD_QUERY)
    write("src/repo.py", BUILD_QUERY_NEAR)
    assert check("SLOP030").findings == []


def test_each_extra_copy_points_at_the_first(write, check):
    """Three copies: the second and third are reported, both pointing at the first."""
    write("a.py", FORMAT_DATE)
    write("b.py", FORMAT_DATE_COPY)
    write("c.py", FORMAT_DATE_COPY.replace("formatDate", "fmt_date"))
    found = messages(check("SLOP030"))
    assert len(found) == 2
    assert all("'format_date' in a.py:1" in m for m in found)


def test_look_alikes_are_not_flagged(write, check):
    """Same shape but different behaviour: other calls, attributes or constants."""
    write("a.py", FORMAT_DATE)
    write(
        "b.py",
        """
def parse_amount(value, currency="EUR"):
    if value is None:
        return 0
    amount = Decimal(value)
    return amount.quantize(CENTS)
""",
    )
    write(
        "c.py",
        """
def format_time(value, fmt="%H:%M"):
    if value is None:
        return "--:--"
    text = value.isoformat(timespec="minutes")
    return text.upper()
""",
    )
    assert check("SLOP030").findings == []


def test_short_functions_are_ignored(write, check):
    """Functions with fewer than 3 statements look alike by nature."""
    write("a.py", "def get_name(self):\n    return self.name\n")
    write("b.py", "def name(obj):\n    return obj.name\n")
    write("c.py", "def f(x):\n    y = x + 1\n    return y\n")
    write("d.py", "def g(z):\n    w = z + 1\n    return w\n")
    assert check("SLOP030").findings == []


def test_test_code_is_ignored(write, check):
    """Repeated setup in tests is normal."""
    write("tests/test_a.py", FORMAT_DATE)
    write("tests/test_b.py", FORMAT_DATE_COPY)
    assert check("SLOP030").findings == []


def test_ignore_directive(write, check):
    """`# slopfence: ignore[SLOP030]` on the def line silences it."""
    write("a.py", FORMAT_DATE)
    write(
        "b.py",
        FORMAT_DATE_COPY.replace(
            'pattern="%Y-%m-%d"):', 'pattern="%Y-%m-%d"):  # slopfence: ignore[SLOP030]'
        ),
    )
    assert check("SLOP030").findings == []


def test_single_file_run_compares_with_whole_project(write, tmp_path):
    """Checking one file (as pre-commit does) still finds a copy elsewhere."""
    write("src/helpers.py", FORMAT_DATE)
    new = write("src/views.py", FORMAT_DATE_COPY)
    result = run([new], tmp_path, OfflineRegistry(), select=["SLOP030"])
    assert [f.path for f in result.findings] == ["src/views.py"]


def test_diff_mode_reports_the_new_copy(write, tmp_path):
    """With --diff, the changed function is reported, even if it sorts first."""

    git = git_project(tmp_path)
    write("src/zz_helpers.py", FORMAT_DATE)
    git("add", "-A")
    git("commit", "-qm", "base")
    write("src/aa_views.py", FORMAT_DATE_COPY)  # sorts before the original
    changed = diffmod.changed_lines(tmp_path, "main")
    result = run([tmp_path], tmp_path, OfflineRegistry(), select=["SLOP030"], changed=changed)
    assert [(f.path, f.line) for f in result.findings] == [("src/aa_views.py", 1)]
    assert "'format_date' in src/zz_helpers.py:1" in result.findings[0].message


def test_nested_function_is_not_its_own_duplicate(write, check):
    """An outer function contains its inner function's code; that's not a copy."""
    write(
        "a.py",
        """
def walk(tree):
    def visit(node):
        for child in node.children:
            if child.skip:
                continue
            yield child
            yield from visit(child)
    yield from visit(tree)
""",
    )
    assert check("SLOP030").findings == []


def test_same_method_name_in_different_classes_is_polymorphism(write, check):
    """Plugins and drivers implement the same method alike on purpose."""
    method = FORMAT_DATE.replace("def format_date(", "    def render(self, ").replace(
        "\n    ", "\n        "
    )
    write("png.py", "class PngImage:\n" + method)
    write("gif.py", "class GifImage:\n" + method)
    assert check("SLOP030").findings == []


def test_identical_methods_with_different_names_are_reported(write, check):
    """Two differently named methods with the same body are a real copy."""
    write(
        "views.py",
        """
class View:
    def show_date(self, value, fmt="%Y-%m-%d"):
        if value is None:
            return ""
        text = value.strftime(fmt)
        return text.strip()

    def display_date(self, d, pattern="%Y-%m-%d"):
        if d is None:
            return ""
        result = d.strftime(pattern)
        return result.strip()
""",
    )
    (msg,) = messages(check("SLOP030"))
    assert "'display_date' is identical to 'show_date'" in msg


def test_mirror_pair_is_not_reported(write, check):
    """Functions that differ in a single name are deliberate parallels (md5 vs sha1)."""
    write(
        "auth.py",
        """
def md5_utf8(text):
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.md5(text).hexdigest()


def sha_utf8(text):
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha1(text).hexdigest()
""",
    )
    assert check("SLOP030").findings == []


def test_near_duplicate_methods_are_not_reported(write, tmp_path):
    """Methods are only reported when identical; near-alike methods are common by design."""
    git = git_project(tmp_path)
    write("db.py", BUILD_QUERY)
    git("add", "-A")
    git("commit", "-qm", "base")
    near = BUILD_QUERY_NEAR.replace("def make_select(", "    def make_select(self, ").replace(
        "\n    ", "\n        "
    )
    write("repo.py", "class Repo:\n" + near)
    changed = diffmod.changed_lines(tmp_path, "main")
    result = run([tmp_path], tmp_path, OfflineRegistry(), select=["SLOP030"], changed=changed)
    assert result.findings == []


def test_parallel_family_is_not_reported(write, check):
    """Same structure, different operator and constants: a deliberate family."""
    write(
        "poly.py",
        """
def polyadd(a1, a2):
    a1, a2 = trim(a1), trim(a2)
    if len(a1) < len(a2):
        a1 = pad(a1, len(a2), 0)
    result = a1 + a2
    return strip_zeros(result, "add")


def polysub(a1, a2):
    a1, a2 = trim(a1), trim(a2)
    if len(a1) < len(a2):
        a1 = pad(a1, len(a2), 1)
    result = a1 - a2
    return strip_zeros(result, "sub")
""",
    )
    assert check("SLOP030").findings == []


def test_alternative_definitions_in_one_file(write, check):
    """The same function defined under if/else is not a copy to remove."""
    body = FORMAT_DATE.replace("\n    ", "\n        ").replace("def ", "    def ")
    write("compat.py", "import sys\n\nif sys.version_info >= (3, 12):\n" + body + "else:\n" + body)
    assert check("SLOP030").findings == []


def test_long_constants_that_differ_late_are_not_identical(write, check):
    """SQL statements sharing a long prefix are different code, not copies."""
    query = """
def {name}(conn, user_id):
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, email, created_at FROM users WHERE {col} = %s", (user_id,))
    row = cursor.fetchone()
    return row
"""
    write("a.py", query.format(name="by_id", col="id"))
    write("b.py", query.format(name="by_team", col="team_id"))
    assert check("SLOP030").findings == []


def test_functions_are_indexed_in_source_order_with_their_class(write, tmp_path):
    """A method belongs to its class, also under an ``if``; a function inside it does not."""
    path = write(
        "shapes.py",
        """
        def top(a):
            b = a + 1
            c = b * 2
            return c


        class Shape:
            def area(self, w):
                def scale(v):
                    x = v * 2
                    y = x + 1
                    return y

                return scale(w)

            if FAST:

                def perimeter(self, w):
                    p = w * 4
                    q = p + 1
                    return q
        """,
    )
    functions = index_functions(load(path, tmp_path))
    assert [(f.name, f.owner) for f in functions] == [
        ("top", None),
        ("area", "Shape"),
        ("scale", None),
        ("perimeter", "Shape"),
    ]


def test_fingerprint_is_the_token_sequence(write, tmp_path):
    """A function's tokens are kept as one string; the tokens and their count come from it."""
    src = load(write("a.py", FORMAT_DATE + FORMAT_DATE_COPY), tmp_path)
    original, copy = index_functions(src)
    assert (original.name, copy.name) == ("format_date", "formatDate")
    assert original.fingerprint == copy.fingerprint
    assert TOKEN_SEP.join(original.tokens) == original.fingerprint
    assert original.size == len(original.tokens) > 10
    assert original.tokens[:3] == ["arguments", "arg", "v0"]


def test_separator_in_a_constant_cannot_fake_a_token_boundary(write, tmp_path, check):
    """A string that holds the separator character is still one token, so two
    different functions never get the same fingerprint."""
    two_constants = """
def labels(value):
    first = str(value)
    second = first.strip()
    return [second, 'a', 'b']
"""
    # One constant whose text is what the two constants above look like when joined.
    text = f"a'{TOKEN_SEP}Constant{TOKEN_SEP}'b"
    one_constant = two_constants.replace("'a', 'b'", repr(text)).replace("labels", "names")
    [two] = index_functions(load(write("a.py", two_constants), tmp_path))
    [one] = index_functions(load(write("b.py", one_constant), tmp_path))
    assert two.tokens[-4:] == ["Constant", "'a'", "Constant", "'b'"]
    # repr() escapes the separator, so the constant splits back into one token: the
    # function has one constant (two tokens) fewer, not as many as the other one.
    assert one.tokens[-2:] == ["Constant", repr(text)]
    assert one.size == len(one.tokens) == two.size - 2
    assert check("SLOP030").findings == []
    write("c.py", two_constants.replace("labels", "captions"))  # a real copy is still found
    assert rule_lines(check("SLOP030"), "SLOP030") == [1]


def test_first_copy_is_the_same_whatever_paths_are_checked(write, tmp_path):
    """Checking one folder reports the same duplicates in it as checking everything.

    The files of the checked paths are read first. If that order picked "the first
    copy", one copy in the folder would go unreported.
    """
    for rel in ["lib/a.py", "svc/b.py", "svc/c.py", "svc/d.py", "svc/e.py"]:
        write(rel, FORMAT_DATE)
    everything = run([tmp_path], tmp_path, OfflineRegistry(), select=["SLOP030"])
    folder = run([tmp_path / "svc"], tmp_path, OfflineRegistry(), select=["SLOP030"])
    assert [f.path for f in folder.findings] == ["svc/b.py", "svc/c.py", "svc/d.py", "svc/e.py"]
    assert folder.findings == everything.findings
    assert all("'format_date' in lib/a.py:1" in m for m in messages(folder))
    one_file = run([tmp_path / "svc" / "b.py"], tmp_path, OfflineRegistry(), select=["SLOP030"])
    assert one_file.findings == everything.findings[:1]


def test_one_order_decides_the_first_copy(write, check):
    """Four copies give three findings, also where files and path texts sort differently.

    Files are found in the order of their folders ("a/" before "a-b/"), while "the
    first copy" is the smallest path text ("a-b/" before "a/").
    """
    for rel in ["a/b.py", "a/d.py", "a/e.py", "a-b/c.py"]:
        write(rel, FORMAT_DATE)
    result = check("SLOP030")
    assert [f.path for f in result.findings] == ["a/b.py", "a/d.py", "a/e.py"]
    assert all("'format_date' in a-b/c.py:1" in m for m in messages(result))


def test_diff_mode_can_also_report_unchanged_copies(write, tmp_path):
    """``report_unchanged`` adds what a full scan finds; a changed first copy names a peer."""
    git = git_project(tmp_path)
    write("src/a.py", FORMAT_DATE)
    write("src/b.py", FORMAT_DATE_COPY)
    write("src/c.py", FORMAT_DATE)
    git("add", ".")
    git("commit", "-q", "-m", "init")
    write("src/a.py", FORMAT_DATE.replace("for display", "for people"))  # the docstring only
    changed = diffmod.changed_lines(tmp_path, "main")
    sources = [load(tmp_path / "src" / f"{name}.py", tmp_path) for name in "abc"]
    functions = [f for src in sources for f in index_functions(src)]
    checked = {src.rel for src in sources}

    [first] = check_duplicates(functions, checked, changed)
    # The changed function is the first copy: a full scan would not report it at all.
    assert (first.path, first.peer) == ("src/a.py", ("src/b.py", "formatDate"))
    both = sorted(
        check_duplicates(functions, checked, changed, report_unchanged=True), key=lambda f: f.path
    )
    assert [(f.path, f.peer) for f in both] == [
        ("src/a.py", ("src/b.py", "formatDate")),
        ("src/b.py", None),
        ("src/c.py", None),
    ]
    # A full scan reports the same two unchanged copies, and it never names a peer.
    full = sorted(check_duplicates(functions, checked), key=lambda f: f.path)
    assert both[1:] == full
    assert all(f.peer is None for f in full)


def test_peer_is_an_unchanged_copy_where_there_is_one(write, tmp_path):
    """Both of the first two copies changed: the peer is the third, which did not."""
    git = git_project(tmp_path)
    for name in "abc":
        write(f"src/{name}.py", FORMAT_DATE)
    git("add", ".")
    git("commit", "-q", "-m", "init")
    for name in "ab":
        write(f"src/{name}.py", FORMAT_DATE.replace("for display", "for people"))
    changed = diffmod.changed_lines(tmp_path, "main")
    sources = [load(tmp_path / "src" / f"{name}.py", tmp_path) for name in "abc"]
    functions = [f for src in sources for f in index_functions(src)]
    found = {f.path: f.peer for f in check_duplicates(functions, {s.rel for s in sources}, changed)}
    # b.py is not the first copy: a full scan reports it, so it needs no peer.
    assert found == {"src/a.py": ("src/c.py", "format_date"), "src/b.py": None}


def test_changed_function_with_only_near_copies_has_no_peer(write, tmp_path):
    """A new function that is a near-copy was never accepted, so nothing stands for it."""
    git = git_project(tmp_path)
    write("src/db.py", BUILD_QUERY)
    git("add", "-A")
    git("commit", "-qm", "base")
    write("src/a_repo.py", BUILD_QUERY_NEAR)
    changed = diffmod.changed_lines(tmp_path, "main")
    result = run([tmp_path], tmp_path, OfflineRegistry(), select=["SLOP030"], changed=changed)
    assert [(f.path, f.peer) for f in result.findings] == [("src/a_repo.py", None)]


def test_edited_first_copy_names_the_copy_it_was_identical_to(write, tmp_path):
    """An edit turns an identical copy into a near-copy. The first of the two has no
    baseline entry of its own, so it names the other one. The second one needs no peer."""
    git = git_project(tmp_path)
    write("src/a.py", BUILD_QUERY)
    write("src/b.py", BUILD_QUERY)
    git("add", "-A")
    git("commit", "-qm", "base")
    edited = BUILD_QUERY.replace(".join(clauses)", ".join(sorted(clauses))")

    def peers():
        """The path and the peer of each finding of a run with ``--diff main``."""
        changed = diffmod.changed_lines(tmp_path, "main")
        result = run([tmp_path], tmp_path, OfflineRegistry(), select=["SLOP030"], changed=changed)
        assert all("% similar to 'build_query'" in f.message for f in result.findings)
        return [(f.path, f.peer) for f in result.findings]

    write("src/a.py", edited)
    assert peers() == [("src/a.py", ("src/b.py", "build_query"))]
    write("src/a.py", BUILD_QUERY)
    write("src/b.py", edited)
    assert peers() == [("src/b.py", None)]
