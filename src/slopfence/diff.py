"""Restrict findings to lines changed relative to a git ref."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The header of a hunk: how many lines of the old file it takes out, where it is in
# the new file, and how many lines it puts in. A count that is left out is 1.
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# How ``git diff`` is asked, so that the answer does not depend on the user's git config.
_DIFF_OPTIONS = (
    "--unified=0",
    "--no-color",
    # The lines of the files as they are: no external diff tool, and no conversion to
    # text first (a ``textconv`` driver, as some tools set one for notebooks).
    "--no-ext-diff",
    "--no-textconv",
    # The prefixes ``parse_unified_diff`` expects, whatever diff.mnemonicPrefix,
    # diff.noprefix, diff.srcPrefix and diff.dstPrefix say.
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


@dataclass(frozen=True)
class Removed:
    """A place where a diff only took lines out of a file, so that no line there is
    new: between line ``after`` and the next one. 0 is the top of the file."""

    after: int


# path (relative to repo root) -> the numbers of its added and changed lines, and the
# places where lines were only removed; None means the whole file is new.
ChangedLines = dict[str, "set[int | Removed] | None"]


# The file name of the git program. Windows adds ``.exe`` to a program that is started
# by its name, and nothing else: a ``git.cmd`` or ``git.bat`` was never started.
_GIT_PROGRAM = "git.exe" if os.name == "nt" else "git"
_NO_GIT = "git is not installed"
# How every git command is started. git reads the config of the repository it runs in,
# and that repository is the project being checked.
_GIT_OPTIONS = (
    # ``core.fsmonitor`` can name a program, which git then runs to ask what changed.
    # The empty value switches it off in every git. ``false`` does not: a git before
    # 2.36 reads the value as the name of a program, and would start one named "false".
    "-c",
    "core.fsmonitor=",
)
# Only read: don't refresh the index file on the side. That write would also run the
# repository's ``post-index-change`` hook. Set in the environment, not with
# ``--no-optional-locks``: a git from before 2.15 turns that option down and ignores
# the variable.
_GIT_ENVIRONMENT = {"GIT_OPTIONAL_LOCKS": "0"}


class DiffError(RuntimeError):
    """git could not give the changed lines: it is not installed, or the ref is unknown."""


def find_git(search_path: str) -> str | None:
    """The full path of the git program in ``search_path``, a ``PATH`` value. None when
    no folder of it holds one.

    Only a folder that is named by its full path counts. An empty entry, ``.`` or
    ``bin`` stands for the current folder or a folder in it, and git runs in the
    project that is checked: a program named git that lies there is a file of that
    project, and slopfence does not start one through such an entry. A folder that is
    named by its full path is trusted as it is, wherever it lies.
    """
    for entry in search_path.split(os.pathsep):
        # Windows takes a folder with quotes around it.
        folder = entry.strip('"')
        # ``Path.is_absolute``, not ``os.path.isabs``: before Python 3.13 that one
        # takes ``\\tools`` on Windows for a full path, and it has no drive.
        if not Path(folder).is_absolute():
            continue
        program = os.path.join(folder, _GIT_PROGRAM)
        if os.path.isfile(program) and os.access(program, os.X_OK):
            return program
    return None


def _git(root: Path, *args: str) -> str:
    """What a git command prints when run in ``root``. Raises DiffError when it fails.

    git is started by its full path (see ``find_git``), so every system starts the
    same program. Started by its name alone, Windows would look for it in the folder
    slopfence was started in before any folder of ``PATH``, and that is the project.
    """
    program = find_git(os.environ.get("PATH", os.defpath))
    if program is None:
        raise DiffError(_NO_GIT)
    try:
        result = subprocess.run(
            [program, *_GIT_OPTIONS, *args],
            cwd=root,
            env={**os.environ, **_GIT_ENVIRONMENT},
            capture_output=True,
            check=True,
        )
    # Removed since it was found a moment ago.
    except FileNotFoundError as err:
        raise DiffError(_NO_GIT) from err
    except subprocess.CalledProcessError as err:
        raise DiffError(_text(err.stderr).strip() or f"git {' '.join(args)} failed") from err
    return _text(result.stdout)


def _text(output: bytes) -> str:
    """What git wrote, as text, byte for byte.

    git emits UTF-8 paths, whatever the locale is (cp1252 on Windows). And it is
    decoded here, not by ``subprocess``: that would turn every carriage return into a
    line end, also one in the middle of a line of a diff.
    """
    return output.decode("utf-8", "surrogateescape")


def repo_root(start: Path) -> Path | None:
    """The top folder of the git repository ``start`` is in, or None outside of one."""
    try:
        return Path(_git(start, "rev-parse", "--show-toplevel").strip())
    except DiffError:
        return None


_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def unquote_git_path(path: str) -> str:
    """Decode git's C-style quoting: "caf\\303\\251.py" -> café.py."""
    if not (len(path) >= 2 and path.startswith('"') and path.endswith('"')):
        return path
    body, out, i = path[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "01234567" and i + 3 < len(body) + 1:
                out.append(int(body[i + 1 : i + 4], 8))
                i += 4
                continue
            out.append(_ESCAPES.get(nxt, ord(nxt)))
            i += 2
            continue
        out.extend(ch.encode())
        i += 1
    return out.decode("utf-8", "surrogateescape")


def _new_path(header: str) -> str | None:
    """The path of the new file that a ``+++ `` line names. None for a deleted file."""
    target = header[4:]
    if not target.startswith('"'):
        # git puts a tab after a name with a space in it. A name that ends in a tab
        # is written in quotes.
        target = target.removesuffix("\t")
    target = unquote_git_path(target)
    return None if target == "/dev/null" else target.removeprefix("b/")


def parse_unified_diff(diff_text: str) -> ChangedLines:
    """What changed in each file of a diff whose new paths start with ``b/``: its added
    and changed lines, and the places where lines were only removed (see ``Removed``).

    The lines of a hunk are counted off by the numbers in its header and never read as
    anything else. They hold the text of the files, and an added line ``++ /dev/null``
    reads ``+++ /dev/null`` in a diff, like the header of a deleted file. For the same
    reason only a line feed ends a line: a carriage return or a form feed in the
    middle of a line must not put the rest of it at the start of a line of its own.
    """
    changed: ChangedLines = {}
    current: str | None = None
    # The lines of the hunk that are still to come: from the old file, and for the new one.
    old = new = 0
    for line in diff_text.split("\n"):
        if old or new:
            if old and line.startswith("-"):
                old -= 1
                continue
            if new and line.startswith("+"):
                new -= 1
                continue
            if line.startswith("\\"):  # "\ No newline at end of file"
                continue
            # Not what git writes in a hunk. The hunk ends here.
            old = new = 0
        if line.startswith("+++ "):
            current = _new_path(line)
            if current is not None:
                changed.setdefault(current, set())
        elif m := _HUNK.match(line):
            old, start, new = (1 if count is None else int(count) for count in m.groups())
            if current is None:
                continue
            lines = changed[current]
            assert lines is not None
            if new:
                lines.update(range(start, start + new))
            else:
                lines.add(Removed(start))
    return changed


def changed_lines(root: Path, ref: str) -> ChangedLines:
    """Lines changed between merge-base(ref, HEAD) and the working tree, plus untracked files.

    Raises DiffError when git can't say, and for a ``ref`` that starts with ``-``: git
    would read it as an option, not as a ref.
    """
    if ref.startswith("-"):
        raise DiffError(f"a git ref can't start with '-' (got {ref!r})")
    base = _git(root, "merge-base", ref, "HEAD").strip()
    diff = _git(root, "diff", *_DIFF_OPTIONS, base)
    changed = parse_unified_diff(diff)
    untracked = _git(root, "ls-files", "-z", "--others", "--exclude-standard")
    for path in untracked.split("\0"):
        if path:
            changed[path] = None
    return changed


def touches(changed: ChangedLines, path: str, line: int, end_line: int | None) -> bool:
    """Whether the diff changed what lies from ``line`` to ``end_line`` of ``path``.

    That is so when one of these lines is new or changed. It is also so when lines
    were removed between two of them: a function that lost a line is a changed
    function, although none of the lines it has left is new. A finding on a single
    line is never touched by a removal, and lines removed just above or below the
    span don't count.
    """
    if path not in changed:
        return False
    lines = changed[path]
    if lines is None:
        return True
    last = end_line or line
    return any(n in lines for n in range(line, last + 1)) or any(
        Removed(n) in lines for n in range(line, last)
    )
