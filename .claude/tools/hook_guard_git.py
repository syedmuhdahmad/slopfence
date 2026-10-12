#!/usr/bin/env python3
"""PreToolUse hook for Bash: no commits on, and no pushes to, a protected branch.

CONTRIBUTING.md says nobody pushes to ``main`` directly. An instruction can be
forgotten; this hook can't. It only looks at this project's repository, and it
lets a command through whenever it can't tell what the command does.

It guards against accidents and is not a security boundary: it reads the command
as text (see ``shellread``) and never runs a shell, so a command built to get past
it can. What refuses a push to ``main`` for good is the repository's ruleset.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

from devenv import (
    PROTECTED_BRANCHES,
    current_branch,
    pre_tool_decision,
    read_event,
    same_repository,
)
from shellread import Variables, expand, split_assignments, split_commands, without_wrappers

# git options that take a value, so the word after them is not the subcommand.
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
# ``git push`` options that take a value, so the word after them is not a remote or refspec.
_PUSH_VALUE_OPTIONS = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
# Programs, and git subcommands, that can create a folder named in their arguments.
_CREATES_FOLDERS = frozenset({"mkdir", "cp", "mv", "ln", "rsync", "tar", "unzip", "install"})
_GIT_CREATES_FOLDERS = frozenset({"clone", "init", "worktree"})
# Shell words that give a variable a value this hook does not follow.
_SETS_VARIABLES = frozenset(
    {"read", "for", "unset", "declare", "local", "typeset", "readonly", "mapfile", "readarray"}
)
# The options ``cd`` takes, alone or joined (``-P``, ``-LP``).
_CD_OPTION = re.compile(r"-[LPe@]+")


class Folders(NamedTuple):
    """The folder the shell is in, and the one ``cd -`` goes back to. None: it can't be known."""

    current: Path | None
    previous: Path | None = None


def resolve_path(
    where: Path | None, target: str, variables: Variables, *, follow_links: bool = False
) -> Path | None:
    """The path ``target`` names when seen from ``where``, or None if it can't be known.

    ``link/..`` has two readings. ``cd`` works on the words of the path, so for it this
    is the folder that holds ``link``. A program such as git asks the operating system,
    which follows the link first: ``follow_links`` gives that reading.

    The path is not checked: it may not exist.
    """
    expanded = expand(target, variables)
    if expanded is None:
        return None
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        if where is None:
            return None
        path = where / path
    return Path(os.path.realpath(path) if follow_links else os.path.normpath(path))


def cd_operands(args: list[str]) -> tuple[list[str], bool]:
    """Read ``cd [-L|-P] [--] [folder]``: the words after the options, and whether ``-P`` holds.

    With ``-P``, ``cd`` follows links the way a program does. The last of ``-L`` and ``-P`` wins.
    """
    index = 0
    while index < len(args) and _CD_OPTION.fullmatch(args[index]):
        index += 1
    options = "".join(args[:index])
    if args[index : index + 1] == ["--"]:
        index += 1
    return args[index:], options.rfind("P") > options.rfind("L")


def after_cd(folders: Folders, args: list[str], variables: Variables, named: set[Path]) -> Folders:
    """Where the shell is after ``cd`` with these arguments.

    ``cd -`` goes back to the folder the shell was in before the last ``cd`` that worked.

    A ``cd`` that fails leaves the shell where it was: one that names two folders
    ("too many arguments"), or one to a folder that does not exist.
    But the folder may be missing only because an earlier command on this line is
    going to create it (``mkdir demo && cd demo``). ``named`` holds the paths that
    such commands name; a missing folder among them, or above one of them, is unknown.
    """
    operands, follow_links = cd_operands(args)
    if len(operands) > 1:
        return folders
    if not operands:
        path: Path | None = Path.home()
    elif operands[0] == "-":
        path = folders.previous
    else:
        path = resolve_path(folders.current, operands[0], variables, follow_links=follow_links)
    if path is not None and not path.is_dir():
        if not any(path == other or path in other.parents for other in named):
            return folders
        path = None
    return Folders(path, folders.current)


def named_paths(args: list[str], where: Path | None, variables: Variables) -> set[Path]:
    """The paths among a command's arguments, seen from ``where``; options are left out."""
    paths = (resolve_path(where, arg, variables) for arg in args if not arg.startswith("-"))
    return {path for path in paths if path is not None}


def parse_git(
    args: list[str], where: Path | None, variables: Variables
) -> tuple[Path | None, str | None, list[str]]:
    """Read ``git [options] <subcommand> [args]``: where it runs, the subcommand, the rest."""
    index = 0
    while index < len(args) and args[index].startswith("-"):
        option = args[index]
        if option in _GIT_VALUE_OPTIONS and index + 1 < len(args):
            if option == "-C":
                # git stops when the folder is missing, so nothing runs: treat it as unknown.
                path = resolve_path(where, args[index + 1], variables, follow_links=True)
                where = path if path is not None and path.is_dir() else None
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
    folders = Folders(cwd)
    variables: dict[str, str | None] = {}
    named: set[Path] = set()  # paths named by commands that can create them
    for assigned, words in map(split_assignments, split_commands(command)):
        words = without_wrappers(words)
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
            folders = after_cd(folders, words[1:], variables, named)
            continue
        if os.path.basename(words[0]) in _CREATES_FOLDERS:
            named |= named_paths(words[1:], folders.current, variables)
        if words[0] != "git":
            continue
        folder, subcommand, rest = parse_git(words[1:], folders.current, variables)
        if subcommand in _GIT_CREATES_FOLDERS:
            named |= named_paths(rest, folder, variables)
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
