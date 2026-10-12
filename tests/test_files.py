"""Replacing a file in one step: the PyPI cache and the baseline are written this way."""

import os

import pytest

from slopfence.files import replace_text


def test_replace_text_writes_a_new_file_and_replaces_an_old_one(tmp_path):
    """The file holds exactly the text, and nothing else is left in the folder."""
    path = tmp_path / "data.json"
    replace_text(path, "first")
    assert path.read_text(encoding="utf-8") == "first"
    replace_text(path, "second")
    assert path.read_text(encoding="utf-8") == "second"
    assert [p.name for p in tmp_path.iterdir()] == ["data.json"]


def test_replace_text_writes_the_same_bytes_on_every_platform(tmp_path):
    """UTF-8, and "\\n" stays "\\n": Windows must not turn it into "\\r\\n"."""
    path = tmp_path / "data.txt"
    replace_text(path, "café\nline two\n")
    assert path.read_bytes() == b"caf\xc3\xa9\nline two\n"


def test_failed_replace_keeps_the_old_file_and_leaves_no_temporary_file(tmp_path, monkeypatch):
    """The old content survives a failed write, and the error reaches the caller."""
    path = tmp_path / "data.json"
    replace_text(path, "old")

    def fail(src, dst):
        """Stand in for a file that another program holds open."""
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(PermissionError):
        replace_text(path, "new")
    assert path.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["data.json"]


def test_replace_text_in_a_missing_folder_is_an_error(tmp_path):
    """The folder is the caller's business: it is not created here."""
    with pytest.raises(FileNotFoundError):
        replace_text(tmp_path / "missing" / "data.json", "text")
    assert list(tmp_path.iterdir()) == []
