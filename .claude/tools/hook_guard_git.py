#!/usr/bin/env python3
"""PreToolUse hook for Bash: no commits on, and no pushes to, a protected branch.

CONTRIBUTING.md says nobody pushes to ``main`` directly. An instruction can be
forgotten; this hook can't. It only looks at this project's repository, and it
lets a command through whenever it can't tell what the command does.
"""

from __future__ import annotations

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
# git options that take a value, so the word after them is not the subcommand.
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
# ``git push`` options that take a value, so the word after them is not a remote or refspec.
_PUSH_VALUE_OPTIONS = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})


def split_commands(command: str) -> list[list[str]]:
    """Split a shell command line into simple commands, each a list of words."""
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


def _without_assignments(words: list[str]) -> list[str]:
    """Drop leading ``NAME=value`` words, as in ``GIT_EDITOR=true git commit``."""
    index = 0
    while index < len(words) and "=" in words[index] and not words[index].startswith("-"):
        index += 1
    return words[index:]


def parse_git(args: list[str], where: Path) -> tuple[Path, str | None, list[str]]:
    """Read ``git [options] <subcommand> [args]``: where it runs, the subcommand, the rest."""
    index = 0
    while index < len(args) and args[index].startswith("-"):
        option = args[index]
        if option in _GIT_VALUE_OPTIONS and index + 1 < len(args):
            if option == "-C":
                where = where / Path(args[index + 1]).expanduser()
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
    """Why the command must not run, or None if it may."""
    where = cwd
    for words in map(_without_assignments, split_commands(command)):
        if not words:
            continue
        if words[0] == "cd" and len(words) > 1:
            where = where / Path(words[1]).expanduser()
            continue
        if words[0] != "git":
            continue
        folder, subcommand, rest = parse_git(words[1:], where)
        if subcommand not in ("commit", "push") or not same_repository(folder, event):
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
