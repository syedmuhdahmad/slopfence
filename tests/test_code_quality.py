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
