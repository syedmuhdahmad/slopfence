#!/usr/bin/env python3
"""PreToolUse hook for Bash: no commits on, and no pushes to, a protected branch.

CONTRIBUTING.md says nobody pushes to ``main`` directly. An instruction can be
forgotten; this hook can't. It only looks at this project's repository, and it
lets a command through whenever it can't tell what the command does.
"""

from __future__ import annotations

import sys
from pathlib import Path

from devenv import (
    PROTECTED_BRANCHES,
    current_branch,
    pre_tool_decision,
    read_event,
    same_repository,
)
from shellread import Variables, expand, split_assignments, split_commands

# git options that take a value, so the word after them is not the subcommand.
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
# ``git push`` options that take a value, so the word after them is not a remote or refspec.
_PUSH_VALUE_OPTIONS = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
# Shell words that give a variable a value this hook does not follow.
_SETS_VARIABLES = frozenset(
    {"read", "for", "unset", "declare", "local", "typeset", "readonly", "mapfile", "readarray"}
)


def resolve_folder(where: Path | None, target: str, variables: Variables) -> Path | None:
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
    args: list[str], where: Path | None, variables: Variables
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
    variables: dict[str, str | None] = {}
    for assigned, words in map(split_assignments, split_commands(command)):
        if words and words[0] == "export":
            assigned, words = split_assignments(words[1:])
        if not words:
            # NAME=value on its own (or after export) lasts for the rest of the command line.
            # A value that can't be known replaces an earlier one: the old value is gone.
            for name, raw in assigned.items():
                variables[name] = None if raw is None else expand(raw, variables)
            continue
        if words[0] in _SETS_VARIABLES:
            for word in words[1:]:
                variables[word.partition("=")[0]] = None
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
