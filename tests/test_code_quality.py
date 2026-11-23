"""SLOP040 (swallowed exceptions, #21) and SLOP011 (stub functions, #22)."""

import pytest

from tests.conftest import rule_lines

# --- SLOP040 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        "except Exception:\n        pass",
        "except BaseException:\n        pass",
        "except:\n        pass",
        "except Exception as e:\n        ...",
        "except (ValueError, Exception):\n        pass",
        "except builtins.Exception:\n        pass",
    ],
)
def test_swallowed_exception_flagged(write, check, handler):
    """A broad handler that does nothing hides every error."""
    write("app.py", f"def save(user):\n    try:\n        db.save(user)\n    {handler}\n")
    assert rule_lines(check("SLOP040"), "SLOP040") == [4]


@pytest.mark.parametrize(
    "code",
    [
        # Narrow exceptions are deliberate.
        "try:\n    x = d['k']\nexcept KeyError:\n    pass\n",
        "try:\n    import ujson as json\nexcept ImportError:\n    json = None\n",
        # Handlers that do something: log, re-raise, return an error, set state.
        "try:\n    save()\nexcept Exception:\n    log.exception('save failed')\n",
        "try:\n    save()\nexcept Exception:\n    raise\n",
        "def f():\n    try:\n        return save()\n    except Exception:\n        return None\n",
        "try:\n    save()\nexcept Exception:\n    failed = True\n",
        # A comment explains why ignoring is fine.
        "try:\n    save()\nexcept Exception:  # best effort, retried later\n    pass\n",
        "try:\n    save()\nexcept Exception:\n    pass  # the cache is optional\n",
        # Cleanup paths, where ignoring errors is the norm.
        "class C:\n    def __del__(self):\n        try:\n            self.f.flush()\n"
        "            save()\n        except Exception:\n            pass\n",
        "def close(self):\n    try:\n        save()\n    except Exception:\n        pass\n",
        "try:\n    os.unlink(tmp)\nexcept Exception:\n    pass\n",
        "try:\n    conn.close()\n    sock.shutdown(2)\nexcept Exception:\n    pass\n",
        "import atexit\n\n@atexit.register\ndef bye():\n    try:\n        save()\n"
        "    except Exception:\n        pass\n",
        "import atexit\n\ndef bye():\n    try:\n        save()\n    except Exception:\n"
        "        pass\n\natexit.register(bye)\n",
        # contextlib.suppress is an explicit, visible decision.
        "with contextlib.suppress(Exception):\n    save()\n",
    ],
)
def test_swallowed_exception_not_flagged(write, check, code):
    """Narrow, handled, commented and cleanup cases are fine."""
    write("app.py", code)
    assert check("SLOP040").findings == []


def test_swallowed_exception_skips_tests_and_examples(write, check):
    """Tests are SLOP022's job; demo and example code may cut corners."""
    body = "try:\n    save()\nexcept Exception:\n    pass\n"
    write("tests/test_app.py", body)
    write("examples/quickstart.py", body)
    assert check("SLOP040").findings == []


# --- SLOP011 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "line"),
    [
        ('def validate_token(token):\n    """Validate the JWT signature."""\n    return True\n', 3),
        (
            'def get_status(payment_id):\n    """Fetches the payment status from the API."""\n'
            '    return {"status": "ok", "code": 200}\n',
            3,
        ),
        (
            'async def fetch_users() -> list[User]:\n    """Fetch users from the database."""\n'
            "    pass\n",
            3,
        ),
        ('def charge(card):\n    """Placeholder: charge the card."""\n    return None\n', 3),
        (
            "class Billing:\n    def refund(self, order) -> Receipt:\n"
            '        """Refund the order."""\n        ...\n',
            4,
        ),
        ('def rates():\n    """Exchange rates, hardcoded for now."""\n    return [1.0, 2.0]\n', 3),
        ('def send(msg):\n    """TODO: send the email."""\n    pass\n', 3),
        # A stub defined inside another function.
        (
            'def outer():\n    def validate(token):\n        """Validate the token."""\n'
            "        return True\n\n    return validate\n",
            4,
        ),
        # A return type written as a string still promises a value.
        ('def fetch() -> "list[User]":\n    """Fetch users from the database."""\n    pass\n', 3),
    ],
)
def test_stub_function_flagged(write, check, code, line):
    """A docstring that promises work, and a body that fakes it."""
    write("app.py", code)
    assert rule_lines(check("SLOP011"), "SLOP011") == [line]


@pytest.mark.parametrize(
    "code",
    [
        # Real implementations.
        'def validate(token):\n    """Validate the token."""\n    return verify(token, KEY)\n',
        # Constant answers that don't promise work.
        'def supports_color():\n    """Whether colour output is supported."""\n    return True\n',
        'def defaults():\n    """Return the default settings."""\n    return {"retries": 3}\n',
        # Hooks meant to be overridden: no return type, nothing promised.
        'def on_start(self):\n    """Called when the server starts."""\n    pass\n',
        'def process(self, item):\n    """Process one item. Subclasses override this."""\n'
        "    pass\n",
        'def save(self) -> None:\n    """Save the record."""\n    pass\n',
        'def load(self) -> Optional[Data]:\n    """Load the data."""\n    return None\n',
        # Return types that None satisfies, however they are spelled.
        'def load(self) -> Data | None:\n    """Load the data."""\n    return None\n',
        'def load(self) -> "Data | None":\n    """Load the data."""\n    return None\n',
        'def load(self) -> typing.Optional[Data]:\n    """Load the data."""\n    return None\n',
        'def load(self) -> t.Any:\n    """Load the data."""\n    return None\n',
        # No docstring: nothing promised.
        "def validate(token):\n    return True\n",
        # Abstract methods, overloads, properties, Protocols, ABCs, TYPE_CHECKING.
        "from abc import abstractmethod\n\nclass Base:\n    @abstractmethod\n"
        '    def fetch(self) -> Data:\n        """Fetch the data."""\n        ...\n',
        "from typing import overload\n\n@overload\n"
        'def parse(x: str) -> int:\n    """Parse x."""\n    ...\n',
        'class C:\n    @property\n    def ok(self):\n        """Check status."""\n'
        "        return True\n",
        "from typing import Protocol\n\nclass Store(Protocol):\n"
        '    def save(self, x) -> bool:\n        """Save x."""\n        ...\n',
        "from abc import ABC\n\nclass Store(ABC):\n"
        '    def load(self) -> Data:\n        """Load data."""\n        ...\n',
        "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n"
        '    def fetch() -> Data:\n        """Fetch."""\n        ...\n',
        # Raising NotImplementedError is honest, not a stub.
        'def send(msg) -> bool:\n    """Send the message."""\n    raise NotImplementedError\n',
        # Constants that ignore no inputs: a fixed representation or default headers.
        'def headers(self):\n    """Create the default headers."""\n'
        '    return {"Accept": "application/json"}\n',
        # Documented hooks and null objects.
        'def verify_request(self, request):\n    """Verify the request. May be overridden."""\n'
        "    return True\n",
        'def flush(self) -> bool:\n    """Flush output. This version does nothing."""\n    pass\n',
        # Honest descriptions of deliberate doubles.
        'def path(self, x) -> str:\n    """Mock method returning a dummy path."""\n'
        '    return "/tmp"\n',
        # Dunder methods.
        'class C:\n    def __bool__(self):\n        """Check truthiness."""\n        return True\n',
    ],
)
def test_stub_function_not_flagged(write, check, code):
    """Real code, honest constants, hooks and declared interfaces are fine."""
    write("app.py", code)
    assert check("SLOP011").findings == []


def test_type_checking_block_is_skipped_however_it_is_spelled(write, check):
    """``if typing.TYPE_CHECKING:`` holds declarations, like ``if TYPE_CHECKING:``."""
    write(
        "app.py",
        "import typing\n\nif typing.TYPE_CHECKING:\n"
        '    def fetch() -> Data:\n        """Fetch."""\n        ...\n',
    )
    assert check("SLOP011").findings == []


def test_huge_number_in_return_annotation_is_read(write, check):
    """Looking for ``None`` in an annotation never turns a number into text: Python
    refuses that for an int of more than 4,300 digits."""
    huge = "0x" + "f" * 4000
    code = f'def fetch_key() -> Literal[{huge}]:\n    """Fetch the key."""\n    pass\n'
    write("app.py", code)
    result = check("SLOP011")
    assert result.parse_errors == []
    assert rule_lines(result, "SLOP011") == [3]


def test_stub_function_skips_tests(write, check):
    """Fake data is normal in test helpers."""
    write(
        "tests/helpers.py",
        'def fetch_user():\n    """Fetch a fake user."""\n    return {"id": 1}\n',
    )
    assert check("SLOP011").findings == []


@pytest.mark.parametrize(
    "code",
    [
        # An optional dependency that may be missing or broken.
        "try:\n    import ujson as json\nexcept Exception:\n    pass\n",
        "try:\n    from lxml import etree\n    import xmltodict\nexcept Exception:\n    pass\n",
        # Cleanup in a loop.
        "try:\n    for d in reversed(created):\n        os.rmdir(d)\nexcept Exception:\n    pass\n",
    ],
)
def test_swallowed_exception_optional_imports_and_cleanup_loops(write, check, code):
    """Optional imports and cleanup loops ignore errors on purpose."""
    write("app.py", code)
    assert check("SLOP040").findings == []


def test_swallowed_exception_still_flags_loops_doing_real_work(write, check):
    """A loop that saves data is not cleanup."""
    write("app.py", "try:\n    for u in users:\n        save(u)\nexcept Exception:\n    pass\n")
    assert rule_lines(check("SLOP040"), "SLOP040") == [4]


def test_stub_function_skips_pyi_files(tmp_path):
    """.pyi files are interface stubs: never reported, even if one reaches the detector."""
    import ast

    from slopfence.detectors.code_quality import check_stub_functions
    from slopfence.engine import discover_all
    from slopfence.source import SourceFile

    text = 'def fetch(user_id) -> dict:\n    """Fetch the user."""\n    ...\n'
    path = tmp_path / "api.pyi"
    path.write_text(text)
    src = SourceFile(path=path, rel="api.pyi", text=text, tree=ast.parse(text))
    assert list(check_stub_functions(src)) == []
    # Discovery only picks up .py files, so a full run never sees it either.
    assert discover_all([tmp_path], tmp_path) == ([], [], [])


# --- A fixed answer in a family of handler methods is no stub (#100). ---


def _handlers(names, fixed, params="self, schema", real="        return build(schema, {name!r})"):
    """A class with one method per name. The ones in ``fixed`` return a constant."""
    lines = ["class Generator:"]
    for name in names:
        lines.append(f"    def {name}({params}):")
        lines.append(f'        """Generates the schema for {name}."""')
        lines.append("        return {'type': 'x'}" if name in fixed else real.format(name=name))
        lines.append("")
    return "\n".join(lines)


KINDS = ["none", "bool", "int", "float", "str", "bytes", "list", "dict"]


def test_fixed_answer_in_a_family_of_handlers_is_not_flagged(write, check):
    """Eight ``*_schema(self, schema)`` methods, six of which build their answer: the
    fixed answers of the two simplest ones are finished code. Seen in pydantic."""
    names = [f"{kind}_schema" for kind in KINDS]
    write("src/app.py", _handlers(names, fixed={"none_schema", "bool_schema"}))
    assert check("SLOP011").findings == []


def test_family_can_share_its_first_word(write, check):
    """``visit_*``, ``handle_*``: the shared word may also come first."""
    names = [f"visit_{kind}" for kind in KINDS]
    code = _handlers(names, fixed={"visit_none"}, params="self, node").replace("schema", "node")
    write("src/app.py", code)
    assert check("SLOP011").findings == []


def test_family_of_stubs_is_still_flagged(write, check):
    """When most of the family returns a constant, nothing shows that it is on purpose."""
    names = [f"{kind}_schema" for kind in KINDS]
    write("src/app.py", _handlers(names, fixed=set(names[:3])))
    assert len(check("SLOP011").findings) == 3
    write("src/app.py", _handlers(names, fixed=set(names)))
    assert len(check("SLOP011").findings) == 8


def test_small_family_is_still_flagged(write, check):
    """Four methods are no family: a stub among them is reported."""
    names = [f"{kind}_schema" for kind in KINDS[:4]]
    write("src/app.py", _handlers(names, fixed={"none_schema"}))
    assert [f.subject for f in check("SLOP011").findings] == ["none_schema"]


def test_methods_with_other_parameters_are_no_family(write, check):
    """``get_user(self, user_id)`` and ``get_token(self, user)`` only share a word.
    Handlers that a dispatcher calls take the same parameters."""
    code = "class Service:\n"
    for n, kind in enumerate(KINDS):
        code += f"    def get_{kind}(self, {kind}_id):\n"
        code += f'        """Fetch the {kind} from the database."""\n'
        code += (
            "        return {'id': 1}\n\n"
            if n == 0
            else f"        return self.db.get({kind}_id)\n\n"
        )
    write("src/app.py", code)
    assert [f.subject for f in check("SLOP011").findings] == ["get_none"]


def test_overload_declarations_do_not_make_a_family(write, check):
    """Four ``@overload`` declarations and one implementation are one method, not five
    handlers: its fixed answer is reported."""
    code = "from typing import overload\n\n\nclass Visitor:\n"
    for kind in KINDS[:4]:
        code += f"    @overload\n    def visit_node(self, node: {kind}) -> bool: ...\n\n"
    code += (
        '    def visit_node(self, node):\n        """Validates the node."""\n        return True\n'
    )
    write("src/app.py", code)
    assert [f.subject for f in check("SLOP011").findings] == ["visit_node"]


def test_abstract_declarations_are_no_family_members(write, check):
    """Four abstract ``visit_*`` methods say what a subclass must write. They are no
    handlers that do real work, so the one fixed answer next to them is reported."""
    code = "from abc import abstractmethod\n\n\nclass Visitor(Base):\n"
    for kind in KINDS[1:5]:
        code += f"    @abstractmethod\n    def visit_{kind}(self, node):\n"
        code += f'        """Generates the node for {kind}."""\n\n'
    code += '    def visit_none(self, node):\n        """Generates the node for none."""\n'
    code += "        return {'type': 'x'}\n"
    write("src/app.py", code)
    assert [f.subject for f in check("SLOP011").findings] == ["visit_none"]


def test_stub_with_other_parameters_does_not_count_against_a_family(write, check):
    """Five ``*_schema(self, schema)`` methods with one fixed answer are a family. A
    stub of the same class that takes another parameter is no member: it is reported
    itself, and it does not take the family's exemption away."""
    names = [f"{kind}_schema" for kind in KINDS[:5]]
    code = _handlers(names, fixed={"none_schema"})
    code += '    def other_schema(self, extra):\n        """Generates the schema for other."""\n'
    code += "        return {'type': 'x'}\n"
    write("src/app.py", code)
    assert [f.subject for f in check("SLOP011").findings] == ["other_schema"]


def test_method_that_does_nothing_in_a_family_is_still_flagged(write, check):
    """A family explains a fixed answer. A method that promises a value and only
    says ``pass`` is a missing implementation, in a family as anywhere."""
    names = [f"visit_{kind}" for kind in KINDS[1:5]]
    code = _handlers(names, fixed=set(), params="self, node").replace("schema", "node")
    code += '    def visit_none(self, node) -> dict:\n        """Generates the node for none."""\n'
    code += "        pass\n"
    write("src/app.py", code)
    assert [f.subject for f in check("SLOP011").findings] == ["visit_none"]


def test_functions_of_a_module_are_no_family(write, check):
    """The rule is about the methods of one class."""
    code = ""
    for n, kind in enumerate(KINDS):
        code += f"def {kind}_schema(schema):\n"
        code += f'    """Generates the schema for {kind}."""\n'
        code += "    return {'type': 'x'}\n\n" if n == 0 else "    return build(schema)\n\n"
    write("src/app.py", code)
    assert [f.subject for f in check("SLOP011").findings] == ["none_schema"]
