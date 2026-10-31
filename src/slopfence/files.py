"""Writing a file so that nobody ever reads half of it."""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path


def _write(path: Path, text: str) -> None:
    """Write ``text`` to the file ``path`` as UTF-8, with the same bytes on every platform."""
    # newline="\n": the same bytes on Windows as everywhere else.
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def replace_text(path: Path, text: str, mode: int | None = None) -> None:
    """Put ``text`` in the file ``path`` in one step, as UTF-8.

    The text goes to a temporary file in the same folder, which then takes the place
    of ``path``. Another process reads the old file or the new one, never a part of
    one. A write that fails or is interrupted leaves the old file as it was. ``mode``,
    when given, is the permissions the new file gets. Raises OSError when the file
    can't be written.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        if mode is not None:
            # Permissions first, text second: a file that only its owner may read is
            # never readable by others, not even while it is written.
            tmp.touch()
            os.chmod(tmp, mode)
        _write(tmp, text)
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
