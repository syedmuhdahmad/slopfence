from __future__ import annotations

import os
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from slopfence import indexes
from slopfence.engine import Result, run
from slopfence.registry import OfflineRegistry


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
def write(tmp_path: Path) -> Callable[[str, str], Path]:
    def _write(rel: str, content: str) -> Path:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"))
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
