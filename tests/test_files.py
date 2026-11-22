"""Replacing a file in one step: the PyPI cache and the baseline are written this way."""

import os
import stat

import pytest

from slopfence import files as filesmod
from slopfence.files import replace_text, rewrite_text

posix = pytest.mark.skipif(os.name == "nt", reason="Windows files have no POSIX permissions")
# root may write whatever the permissions say, so a test of a refused write proves nothing.
not_root = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="needs a user that can be refused"
)


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


@pytest.mark.skipif(os.name == "nt", reason="Windows files have no POSIX permissions")
def test_replace_text_sets_the_mode_before_the_text_goes_in(tmp_path, monkeypatch):
    """A file that only its owner may read never holds the text with wider permissions."""
    sizes = []
    chmod = os.chmod

    def chmod_and_record(path, mode):
        """Note how much text the file holds at the moment its permissions are set."""
        sizes.append(os.stat(path).st_size)
        chmod(path, mode)

    monkeypatch.setattr(os, "chmod", chmod_and_record)
    replace_text(tmp_path / "report.json", "secret", 0o600)
    assert sizes == [0]
    assert stat.S_IMODE((tmp_path / "report.json").stat().st_mode) == 0o600


def _fixed_temporary_name(monkeypatch, token="0123456789abcdef"):
    """Make the name of the temporary file known, as if somebody had guessed it."""
    monkeypatch.setattr(filesmod.secrets, "token_hex", lambda size: token)
    return f"data.json.{token}.tmp"


@pytest.mark.skipif(os.name == "nt", reason="making a symbolic link needs a privilege on Windows")
def test_replace_text_never_writes_through_a_link_that_was_put_in_its_way(tmp_path, monkeypatch):
    """A link with the temporary file's name is not followed: the file it names is untouched."""
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("mine", encoding="utf-8")
    planted = tmp_path / _fixed_temporary_name(monkeypatch)
    planted.symlink_to(elsewhere)
    with pytest.raises(FileExistsError):
        replace_text(tmp_path / "data.json", "report")
    assert elsewhere.read_text(encoding="utf-8") == "mine"
    assert not (tmp_path / "data.json").exists()
    assert planted.is_symlink(), "a file that replace_text did not make is not its to remove"


def test_replace_text_never_overwrites_a_file_with_the_temporary_name(tmp_path, monkeypatch):
    """The temporary file is always a new one: a file of that name is left as it is."""
    in_the_way = tmp_path / _fixed_temporary_name(monkeypatch)
    in_the_way.write_text("not ours", encoding="utf-8")
    with pytest.raises(FileExistsError):
        replace_text(tmp_path / "data.json", "report")
    assert in_the_way.read_text(encoding="utf-8") == "not ours"


def test_temporary_file_name_is_new_every_time(tmp_path, monkeypatch):
    """The name can't be worked out in advance, so nobody can put a file in its way."""
    used = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda src, dst: used.append(src.name) or real(src, dst))
    for _ in range(3):
        replace_text(tmp_path / "data.json", "text")
    assert len(set(used)) == 3
    assert all(name.startswith("data.json.") and name.endswith(".tmp") for name in used)


def test_replace_text_in_a_missing_folder_is_an_error(tmp_path):
    """The folder is the caller's business: it is not created here."""
    with pytest.raises(FileNotFoundError):
        replace_text(tmp_path / "missing" / "data.json", "text")
    assert list(tmp_path.iterdir()) == []


@posix
def test_temporary_file_is_made_with_its_permissions(tmp_path, monkeypatch):
    """There is no moment in which another user could open the temporary file: it has
    its permissions from the start, not from a later step."""
    made_with = []
    chmod = os.chmod

    def record_then_chmod(path, mode):
        """Note the permissions the file was made with, before they are set again."""
        made_with.append(stat.S_IMODE(os.stat(path).st_mode))
        chmod(path, mode)

    monkeypatch.setattr(os, "chmod", record_then_chmod)
    before = os.umask(0o022)
    try:
        replace_text(tmp_path / "cache.json", "names", 0o600)
    finally:
        os.umask(before)
    assert made_with == [0o600]


@posix
def test_replace_text_gives_the_mode_that_the_umask_would_cut(tmp_path):
    """``mode`` is what the file gets, also when the umask allows less for a new file."""
    before = os.umask(0o077)
    try:
        replace_text(tmp_path / "report.json", "text", 0o644)
    finally:
        os.umask(before)
    assert stat.S_IMODE((tmp_path / "report.json").stat().st_mode) == 0o644


def test_rewrite_text_writes_a_new_file_and_replaces_an_old_one(tmp_path):
    """Like ``replace_text``: the file holds the new text, and no temporary file is left."""
    path = tmp_path / "pypi.json"
    rewrite_text(path, "one")
    rewrite_text(path, "two")
    assert path.read_text(encoding="utf-8") == "two"
    assert [p.name for p in tmp_path.iterdir()] == ["pypi.json"]


@posix
def test_rewrite_text_keeps_the_permissions_of_the_file(tmp_path):
    """A file that was made private is still private after it was rewritten."""
    path = tmp_path / "pypi.json"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o600)
    rewrite_text(path, "new")
    assert path.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="making a symbolic link needs a privilege on Windows")
def test_rewrite_text_replaces_a_link_and_leaves_the_file_it_names(tmp_path):
    """A link in the file's place can be somebody else's: the file it names is not
    written. The link makes way for the new file."""
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("mine", encoding="utf-8")
    path = tmp_path / "pypi.json"
    path.symlink_to(elsewhere)
    rewrite_text(path, "cache")
    assert elsewhere.read_text(encoding="utf-8") == "mine"
    assert not path.is_symlink()
    assert path.read_text(encoding="utf-8") == "cache"


@posix
@not_root
def test_rewrite_text_leaves_a_file_that_may_not_be_written(tmp_path):
    """A read-only file keeps its protection: an error, the old text, the old permissions."""
    path = tmp_path / "pypi.json"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o444)
    with pytest.raises(PermissionError):
        rewrite_text(path, "new")
    assert path.read_text(encoding="utf-8") == "old"
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert [p.name for p in tmp_path.iterdir()] == ["pypi.json"]


@posix
@not_root
def test_rewrite_text_never_writes_in_place(tmp_path):
    """In a folder that takes no temporary file the write fails, and the old file is
    whole: it was not emptied first."""
    folder = tmp_path / "locked"
    folder.mkdir()
    path = folder / "pypi.json"
    path.write_text("old", encoding="utf-8")
    folder.chmod(0o555)
    try:
        with pytest.raises(PermissionError):
            rewrite_text(path, "new")
        assert path.read_text(encoding="utf-8") == "old"
    finally:
        folder.chmod(0o755)
