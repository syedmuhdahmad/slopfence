"""Just enough reading of a shell command line for the git guard hook.

This is not a shell parser. It splits a command line into simple commands, leaves
out here-document text the shell would only pass on as data, and fills in variables
set on the same line. Where it can't be sure of a value it says so with None, and
the caller must not guess.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass

# Shell operators, backticks and line breaks end one simple command and start the next.
PUNCTUATION = "();<>|&`\n"
# What follows "<<": an optional "-", then the closing word, quoted or bare. A bare word
# must start like a name, so that a shift (``1 << 4``) is not taken for a here-document.
_CLOSING_WORD = re.compile(
    r"(-?)[ \t]*(?:'([^']*)'|\"([^\"]*)\"|(\\?)([A-Za-z_][^\s;&|<>()'\"`]*))"
)
# A leading NAME=value or NAME+=value word, and a $NAME or ${NAME} reference.
_ASSIGNMENT = re.compile(r"([A-Za-z_]\w*)(\+?)=(.*)", re.S)
_VARIABLE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
# Programs that run their input as a script: a here-document given to one is commands.
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_WORD_BREAK = re.compile(r"[\s;&|()<>]+")

# name -> value for variables set on the command line; None for a value that can't be known.
Variables = Mapping[str, "str | None"]


@dataclass(frozen=True)
class HereDocument:
    """A here-document opened on a line: how it ends and whether the shell expands it."""

    closing_word: str
    # ``<<-``: the shell strips leading tabs, also from the closing line.
    tabs_allowed: bool
    # A quoted closing word: the body is passed on as it is, nothing in it runs.
    literal: bool


def _read_line(line: str, quotes: list[str]) -> tuple[str, list[HereDocument]]:
    """One line of a command: its text without a trailing comment, and the here-documents it opens.

    ``quotes`` is the quoting the line starts in, innermost last ("" outside quotes,
    one entry more for each ``$(`` that is open). It is updated in place, because a
    quoted string can run over several lines. ``<<`` and ``#`` inside quotes are
    text, not an operator and not the start of a comment.
    """
    found: list[HereDocument] = []
    index = 0
    while index < len(line):
        char, quote = line[index], quotes[-1]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote == "'":
            if char == "'":
                quotes[-1] = ""
        elif line.startswith("$((", index) and (end := line.find("))", index)) != -1:
            index = end + 1  # arithmetic: ``<<`` in it is a shift
        elif line.startswith("$(", index):
            quotes.append("")
            index += 1
        elif char == '"':
            quotes[-1] = "" if quote else '"'
        elif quote == "":
            if char == "'":
                quotes[-1] = "'"
            elif char == ")" and len(quotes) > 1:
                quotes.pop()
            elif char == "#" and (index == 0 or line[index - 1].isspace()):
                return line[:index], found  # a comment: the rest of the line is not code
            elif line.startswith("<<<", index):
                index += 2  # a here-string, not a here-document
            elif line.startswith("<<", index) and (match := _CLOSING_WORD.match(line, index + 2)):
                quoted = match.group(2) is not None or match.group(3) is not None
                word = match.group(2) or match.group(3) or match.group(5) or ""
                found.append(
                    HereDocument(word, bool(match.group(1)), quoted or bool(match.group(4)))
                )
                index = match.end() - 1
        index += 1
    return line, found


def _runnable(body: list[str], document: HereDocument, feeds_a_shell: bool) -> list[str]:
    """The lines of a here-document body that the shell may run as commands."""
    if feeds_a_shell:
        return body  # the body is a script for that shell
    if document.literal:
        return []
    # An unquoted here-document is expanded: command substitutions in it run.
    return [line for line in body if "$(" in line or "`" in line]


def strip_heredocs(command: str) -> str:
    """Leave out comments, and here-document text that the shell only passes on as data.

    Without this, a script or a note that merely mentions a commit command would be
    read as one. The line that opens a here-document is kept. So is a body the shell
    does run: one given to a shell (``bash <<EOF``), and the lines of an unquoted
    here-document that hold a command substitution.
    """
    lines = command.split("\n")
    kept: list[str] = []
    quotes = [""]
    index = 0
    while index < len(lines):
        line, documents = _read_line(lines[index], quotes)
        index += 1
        kept.append(line)
        feeds_a_shell = any(os.path.basename(word) in _SHELLS for word in _WORD_BREAK.split(line))
        for document in documents:
            body: list[str] = []
            while index < len(lines):
                text = lines[index]
                index += 1
                if (text.lstrip("\t") if document.tabs_allowed else text) == document.closing_word:
                    break
                body.append(text)
            kept.extend(_runnable(body, document, feeds_a_shell))
    return "\n".join(kept)


def split_commands(command: str) -> list[list[str]]:
    """Split a shell command line into simple commands, each a list of words."""
    command = strip_heredocs(command)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=PUNCTUATION)
    # Comments are already cut. shlex would swallow the line break after one, and with
    # it the border between that line's command and the next.
    lexer.commenters = ""
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        # Unbalanced quotes: split on blanks instead, so a command is still seen.
        tokens = command.replace("\n", " ; ").split()
    commands: list[list[str]] = [[]]
    for token in tokens:
        if set(token) <= set(PUNCTUATION):
            commands.append([])
        else:
            commands[-1].append(token)
    return [words for words in commands if words]


def split_assignments(words: list[str]) -> tuple[dict[str, str | None], list[str]]:
    """Separate leading ``NAME=value`` words from the command that follows them.

    The value is None for ``NAME+=value``: what it adds to is not followed.
    """
    assigned: dict[str, str | None] = {}
    for index, word in enumerate(words):
        match = _ASSIGNMENT.fullmatch(word)
        if match is None:
            return assigned, words[index:]
        assigned[match.group(1)] = None if match.group(2) else match.group(3)
    return assigned, []


def expand(text: str, variables: Variables) -> str | None:
    """``text`` with ``$NAME`` and ``${NAME}`` filled in, or None if a part can't be known."""
    unknown = False

    def value(match: re.Match[str]) -> str:
        """The value of one variable: from this command line if set there, else the environment."""
        nonlocal unknown
        name = match.group(1) or match.group(2)
        found = variables[name] if name in variables else os.environ.get(name)
        unknown = unknown or found is None
        return found or ""

    expanded = _VARIABLE.sub(value, text)
    # Anything still unexpanded (a command substitution, a special parameter) can't be known.
    return None if unknown or "$" in expanded or "`" in expanded else expanded
