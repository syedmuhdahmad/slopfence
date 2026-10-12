"""Restrict findings to lines changed relative to a git ref."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

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

# path (relative to repo root) -> changed line numbers; None means the whole file is new.
ChangedLines = dict[str, "set[int] | None"]


class DiffError(RuntimeError):
    """git could not give the changed lines: it is not installed, or the ref is unknown."""


def _git(root: Path, *args: str) -> str:
    """What a git command prints when run in ``root``. Raises DiffError when it fails."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            check=True,
            # git emits UTF-8 paths; don't decode with the locale (cp1252 on Windows).
            encoding="utf-8",
            errors="surrogateescape",
        )
    except FileNotFoundError as err:
        raise DiffError("git is not installed") from err
    except subprocess.CalledProcessError as err:
        raise DiffError(err.stderr.strip() or f"git {' '.join(args)} failed") from err
    return result.stdout


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


def parse_unified_diff(diff_text: str) -> ChangedLines:
    """The added and changed lines of each file in a diff whose new paths start with ``b/``."""
    changed: ChangedLines = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = unquote_git_path(line[4:].rstrip("\n"))
            current = None if target == "/dev/null" else target.removeprefix("b/")
            if current is not None:
                changed.setdefault(current, set())
        elif current is not None and (m := _HUNK.match(line)):
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            lines = changed[current]
            assert lines is not None
            lines.update(range(start, start + count))
    return changed


def changed_lines(root: Path, ref: str) -> ChangedLines:
    """Lines changed between merge-base(ref, HEAD) and the working tree, plus untracked files."""
    base = _git(root, "merge-base", ref, "HEAD").strip()
    diff = _git(root, "diff", *_DIFF_OPTIONS, base)
    changed = parse_unified_diff(diff)
    untracked = _git(root, "ls-files", "-z", "--others", "--exclude-standard")
    for path in untracked.split("\0"):
        if path:
            changed[path] = None
    return changed


def touches(changed: ChangedLines, path: str, line: int, end_line: int | None) -> bool:
    """Whether any line from ``line`` to ``end_line`` of ``path`` was changed."""
    if path not in changed:
        return False
    lines = changed[path]
    if lines is None:
        return True
    return any(n in lines for n in range(line, (end_line or line) + 1))
