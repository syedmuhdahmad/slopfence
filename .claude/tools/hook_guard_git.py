#!/usr/bin/env python3
"""PreToolUse hook for Bash: no commits on, and no pushes to, a protected branch.

CONTRIBUTING.md says nobody pushes to ``main`` directly. An instruction can be
forgotten; this hook can't. It only looks at this project's repository, and it
lets a command through whenever it can't tell what the command does.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path

from devenv import (
    PROTECTED_BRANCHES,
    current_branch,
    pre_tool_decision,
    read_event,
    same_repository,
)

# Shell operators and line breaks end one simple command and start the next.
_PUNCTUATION = "();<>|&\n"
# The start of a here-document: <<WORD, <<-WORD, <<'WORD' or <<"WORD", but not <<< (a here-string).
_HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(['\"]?)([A-Za-z_][\w.-]*)\2")
# A leading NAME=value word, and a $NAME or ${NAME} reference.
_ASSIGNMENT = re.compile(r"([A-Za-z_]\w*)=(.*)", re.S)
_VARIABLE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
# git options that take a value, so the word after them is not the subcommand.
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
# ``git push`` options that take a value, so the word after them is not a remote or refspec.
_PUSH_VALUE_OPTIONS = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})


def strip_heredocs(command: str) -> str:
    """Drop the bodies of here-documents: they are data fed to a command, not commands.

    Without this, a script or a note that merely mentions a commit command would be
    read as one. The line that opens the here-document is kept.
    """
    kept: list[str] = []
    open_documents: list[tuple[str, bool]] = []  # (closing word, leading tabs allowed), in order
    for line in command.split("\n"):
        if open_documents:
            word, tabs_allowed = open_documents[0]
            if (line.lstrip("\t") if tabs_allowed else line) == word:
                open_documents.pop(0)
            continue
        kept.append(line)
        open_documents = [(m.group(3), bool(m.group(1))) for m in _HEREDOC.finditer(line)]
    return "\n".join(kept)


def split_commands(command: str) -> list[list[str]]:
    """Split a shell command line into simple commands, each a list of words."""
    command = strip_heredocs(command)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        # Unbalanced quotes (an apostrophe in a here-document): split on blanks instead.
        tokens = command.replace("\n", " ; ").split()
    commands: list[list[str]] = [[]]
    for token in tokens:
        if set(token) <= set(_PUNCTUATION):
            commands.append([])
        else:
            commands[-1].append(token)
    return [words for words in commands if words]


def split_assignments(words: list[str]) -> tuple[dict[str, str], list[str]]:
    """Separate leading ``NAME=value`` words from the command that follows them."""
    assigned: dict[str, str] = {}
    for index, word in enumerate(words):
        match = _ASSIGNMENT.fullmatch(word)
        if match is None:
            return assigned, words[index:]
        assigned[match.group(1)] = match.group(2)
    return assigned, []


def expand(text: str, variables: dict[str, str]) -> str | None:
    """``text`` with ``$NAME`` and ``${NAME}`` filled in, or None if a part can't be known."""
    unknown = False

    def value(match: re.Match[str]) -> str:
        """The value of one variable, from this command line first, then the environment."""
        nonlocal unknown
        name = match.group(1) or match.group(2)
        found = variables.get(name, os.environ.get(name))
        unknown = unknown or found is None
        return found or ""

    expanded = _VARIABLE.sub(value, text)
    # Anything still unexpanded (a command substitution, a special parameter) can't be known.
    return None if unknown or "$" in expanded or "`" in expanded else expanded


def resolve_folder(where: Path | None, target: str, variables: dict[str, str]) -> Path | None:
    """The folder ``target`` names when seen from ``where``, or None if it can't be known."""
    expanded = expand(target, variables)
    if expanded is None:
        return None
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        if where is None:
            return None
        path = where / path
    return path if path.is_dir() else None


def parse_git(
    args: list[str], where: Path | None, variables: dict[str, str]
) -> tuple[Path | None, str | None, list[str]]:
    """Read ``git [options] <subcommand> [args]``: where it runs, the subcommand, the rest."""
    index = 0
    while index < len(args) and args[index].startswith("-"):
        option = args[index]
        if option in _GIT_VALUE_OPTIONS and index + 1 < len(args):
            if option == "-C":
                where = resolve_folder(where, args[index + 1], variables)
            index += 1
        index += 1
    if index >= len(args):
        return where, None, []
    return where, args[index], args[index + 1 :]


def positional_words(args: list[str], value_options: frozenset[str]) -> list[str]:
    """The words that are neither options nor the value of an option that takes one."""
    words, skip_next = [], False
    for arg in args:
        if skip_next:
            skip_next = False
        elif arg in value_options:
            skip_next = True
        elif not arg.startswith("-"):
            words.append(arg)
    return words


def push_target(args: list[str], where: Path) -> str | None:
    """The protected branch a ``git push`` would update, or None."""
    refspecs = positional_words(args, _PUSH_VALUE_OPTIONS)[1:]  # the first word is the remote
    if not refspecs:
        branch = current_branch(where)
        return branch if branch in PROTECTED_BRANCHES else None
    for spec in refspecs:
        target = spec.rsplit(":", 1)[-1].lstrip("+").removeprefix("refs/heads/")
        if target == "HEAD":
            target = current_branch(where) or ""
        if target in PROTECTED_BRANCHES:
            return target
    return None


def find_violation(command: str, cwd: Path, event: dict) -> str | None:
    """Why the command must not run, or None if it may.

    The folder each git command runs in is followed through ``cd``, ``git -C`` and
    variables set on the same command line. When that folder can't be known, the
    command is let through: a guess could block a commit on the right branch.
    """
    where: Path | None = cwd
    variables: dict[str, str] = {}
    for assigned, words in map(split_assignments, split_commands(command)):
        if words and words[0] == "export":
            assigned, words = split_assignments(words[1:])
        if not words:
            # NAME=value on its own (or after export) lasts for the rest of the command line.
            for name, raw in assigned.items():
                known = expand(raw, variables)
                if known is not None:
                    variables[name] = known
            continue
        if words[0] == "cd":
            where = resolve_folder(where, words[1], variables) if len(words) > 1 else Path.home()
            continue
        if words[0] != "git":
            continue
        folder, subcommand, rest = parse_git(words[1:], where, variables)
        if folder is None or subcommand not in ("commit", "push"):
            continue
        if not same_repository(folder, event):
            continue
        if subcommand == "commit":
            branch = current_branch(folder)
            if branch in PROTECTED_BRANCHES:
                return (
                    f"'{branch}' is protected: changes reach it through pull requests only. "
                    "Create a branch first, for example: git switch -c feat/<short-name> "
                    "(or fix/..., docs/..., chore/...), then commit."
                )
        elif target := push_target(rest, folder):
            return (
                f"Pushing to '{target}' is not allowed: it is protected. Push your branch "
                "(git push -u origin <branch>) and open a pull request instead."
            )
    return None


def main() -> int:
    """Deny the Bash call when it would commit on or push to a protected branch."""
    event = read_event()
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if isinstance(command, str):
        reason = find_violation(command, Path(event.get("cwd") or "."), event)
        if reason:
            pre_tool_decision("deny", reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
