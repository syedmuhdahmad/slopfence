"""Writing a file so that nobody ever reads half of it."""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
from pathlib import Path

# How the temporary file is opened. It must not exist yet, and a link of that name is
# not followed. ``O_BINARY`` is there on Windows only, where a file opened without it
# gets ``\r\n`` for every line end.
_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)


def _write(path: Path, text: str) -> None:
    """Write ``text`` to the file ``path`` as UTF-8, with the same bytes on every platform."""
    # newline="\n": the same bytes on Windows as everywhere else.
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def _set_mode(descriptor: int, path: Path, mode: int) -> None:
    """Give the open file ``descriptor``, made as ``path``, the permissions ``mode``.

    Through the descriptor, not the name: in a folder that others can write to, the
    name can stand for a symbolic link by now, and ``chmod`` on it would change the
    file the link names. Windows has no such permissions, and no ``fchmod`` before
    Python 3.13: there the name is used, and only the read-only flag changes.
    """
    if os.name == "posix":
        os.fchmod(descriptor, mode)
    else:
        os.chmod(path, mode)


def replace_text(path: Path, text: str, mode: int | None = None) -> None:
    """Put ``text`` in the file ``path`` in one step, as UTF-8.

    The text goes to a temporary file in the same folder, which then takes the place
    of ``path``. Another process reads the old file or the new one, never a part of
    one. A write that fails or is interrupted leaves the old file as it was. ``mode``,
    when given, is the permissions the new file gets. Raises OSError when the file
    can't be written.

    The temporary file is always a new file with a name nobody can know in advance.
    The folder can be one that others put files in: the project that is checked, or a
    shared cache folder. A file or a symbolic link that was put there under a name
    that could be guessed would otherwise be written through.
    """
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    # Made with its permissions in the same call: a file that only its owner may read
    # is never open to others, not even while it is empty. Somebody who opened it in
    # that moment could read the text that is written afterwards. Opened before the
    # ``try``, so that a file that was already there is not removed below.
    descriptor = os.open(tmp, _NEW_FILE, 0o666 if mode is None else mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            if mode is not None:
                # The umask can have taken permissions away from the new file. This
                # gives it exactly ``mode``, and never more than was asked for.
                _set_mode(descriptor, tmp, mode)
            stream.write(text)
        os.replace(tmp, path)
    finally:
        # Gone already when the write worked. Left behind when it failed.
        with contextlib.suppress(OSError):
            tmp.unlink()


def write_text(path: Path, text: str) -> None:
    """Put ``text`` in ``path``: in one step where that is safe, in place elsewhere.

    A file that is not there yet, or a regular file that may be written in a folder
    that may be written, is replaced by :func:`replace_text`, so a reader sees the old
    file or the new one. A symbolic link is followed: the file it names is updated,
    keeps its permissions, and the link stays a link. Anything else is opened and
    written in place, as a plain ``open`` would: a pipe or a device (``/dev/stdout``,
    ``/dev/null``, ``NUL``) has no directory entry to swap, a file in a folder that
    may not be written can't get a temporary file next to it, and a file that may not
    be written must keep its protection, so the write fails. Raises OSError when the
    file can't be written.
    """
    try:
        # The path as given, not its realpath: /dev/stdout on a pipe resolves to a
        # name that is not there, while stat still sees the pipe.
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        mode = None
    target = Path(os.path.realpath(path))
    if mode is None:
        replace_text(target, text)
        return
    writable = os.access(target, os.W_OK) and os.access(target.parent, os.W_OK)
    if stat.S_ISREG(mode) and writable:
        replace_text(target, text, stat.S_IMODE(mode))
        return
    _write(path, text)


def rewrite_text(path: Path, text: str) -> None:
    """Put ``text`` in the file ``path`` in one step, and keep the permissions it has.

    For a file of slopfence's own in a folder that others may be able to write to: the
    PyPI cache. It is stricter than :func:`write_text` in two ways. A symbolic link at
    ``path`` is replaced by the file and never followed: somebody else can have put it
    there, and the file it names is not slopfence's to write. And nothing is written in
    place: where no temporary file can be made next to it, the old file stays whole.

    A regular file keeps its permissions. One that may not be written keeps its
    protection too: that is a PermissionError. Raises OSError when the file can't be
    written.
    """
    try:
        found = os.lstat(path).st_mode
    except FileNotFoundError:
        found = None
    mode = None
    if found is not None and stat.S_ISREG(found):
        if not os.access(path, os.W_OK):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
        mode = stat.S_IMODE(found)
    replace_text(path, text, mode)
