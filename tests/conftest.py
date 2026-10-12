from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from slopfence import indexes
from slopfence.detectors import packages
from slopfence.engine import Result, run
from slopfence.registry import OfflineRegistry

# For a test case with an expression that is 100,000 operands long. Before Python 3.11
# ``ast.parse`` has no depth limit: it takes such a sum with about 8 MB of stack, and
# with less (``python.exe`` on Windows has 2 MB) the interpreter dies, and the whole
# test run with it. From 3.11 on the parser gives up with an error, which is the case
# the test is about.
needs_parser_depth_limit = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="ast.parse has no depth limit before Python 3.11"
)


class FakeRegistry:
    """Registry where only the listed packages exist."""

    def __init__(self, existing: set[str]) -> None:
        from slopfence.registry import normalize

        self.existing = {normalize(n) for n in existing}
        self.queries: list[str] = []

    def exists(self, name: str) -> bool | None:
        from slopfence.registry import normalize

        self.queries.append(name)
        return normalize(name) in self.existing


class FakeBatchRegistry(FakeRegistry):
    """FakeRegistry that also takes every name up front, like ``PyPIRegistry.prefetch``."""

    def __init__(self, existing: set[str]) -> None:
        """``batches`` records what each ``prefetch`` call was given."""
        super().__init__(existing)
        self.batches: list[list[str]] = []

    def prefetch(self, names) -> None:
        """Record the batch; the answers still come from ``exists``."""
        self.batches.append(list(names))


class DownRegistry:
    """A registry that can't be reached: it has no answer for any name."""

    def exists(self, name: str) -> bool | None:
        """Always unknown."""
        return None


INDEX_ENV_VARS = (
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "UV_INDEX",
    "UV_DEFAULT_INDEX",
    "UV_INDEX_URL",
    "UV_EXTRA_INDEX_URL",
    "UV_CONFIG_FILE",
)


@pytest.fixture(autouse=True)
def no_index_config(monkeypatch):
    """Keep the machine's pip/uv index settings (pip.conf, PIP_INDEX_URL...) out of tests."""
    for var in INDEX_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("UV_NO_CONFIG", "1")
    # Tests that switch config files back on must never see /etc/pip.conf and friends.
    monkeypatch.setattr(indexes, "PIP_SYSTEM_FILES", ())
    monkeypatch.setattr(indexes, "UV_SYSTEM_FILES", ())


@pytest.fixture
def not_installed(monkeypatch):
    """Pretend nothing is installed, so only declarations can vouch for an import."""
    monkeypatch.setattr(packages, "_installed_modules", set)
    monkeypatch.setattr(packages, "_is_installed", lambda name, installed: False)


@pytest.fixture
def write(tmp_path: Path) -> Callable[[str, str], Path]:
    """A function that writes one file of the project under test, as UTF-8.

    Without a named encoding a file would be written in the encoding of the locale,
    which on Windows is not UTF-8: slopfence could not read a file that holds a
    letter outside ASCII.
    """

    def _write(rel: str, content: str) -> Path:
        """Write ``content`` (dedented) to ``rel`` inside the project and return its path."""
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def check(tmp_path: Path) -> Callable[..., Result]:
    def _check(*rules: str, registry=None, **kwargs) -> Result:
        return run(
            [tmp_path],
            tmp_path,
            registry or OfflineRegistry(),
            select=rules or None,
            **kwargs,
        )

    return _check


def pyproject(root: Path, body: str) -> None:
    """Write the pyproject.toml of a project named "demo". ``body`` is its slopfence part."""
    (root / "pyproject.toml").write_text("[project]\nname = 'demo'\n\n" + body, encoding="utf-8")


def git(cwd: Path, *args: str) -> None:
    """Run a git command in ``cwd``. Fails the test when git does."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def git_init(cwd: Path) -> None:
    """Make ``cwd`` a git repository on the branch main, with a name to commit under."""
    git(cwd, "init", "-q", "-b", "main")
    git(cwd, "config", "user.email", "t@example.com")
    git(cwd, "config", "user.name", "t")


def rule_lines(result: Result, rule: str) -> list[int]:
    return [f.line for f in result.findings if f.rule == rule]
