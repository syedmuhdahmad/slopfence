"""Just enough reading of a shell command line for the git guard hook.

This is not a shell parser. It splits a command line into simple commands, leaves
out comments and here-document text the shell would only pass on as data, and fills
in variables set on the same line. Where it can't be sure of a value it says so with
None, and the caller must not guess.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass

# Shell operators, backticks and line breaks end one simple command and start the next.
PUNCTUATION = "();<>|&`\n"
# What follows "<<": an optional "-", then the closing word, which may be quoted in part.
_CLOSING_WORD = re.compile(r"(-?)[ \t]*((?:'[^']*'|\"[^\"]*\"|\\.|[^\s;&|<>()'\"`\\])+)")
# A leading NAME=value or NAME+=value word, and a $NAME or ${NAME} reference.
_ASSIGNMENT = re.compile(r"([A-Za-z_]\w*)(\+?)=(.*)", re.S)
_VARIABLE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
# Programs that run their input as a script: a here-document given to one is commands.
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
# Words that come before the program they start, as in ``sudo bash``.
_WRAPPERS = frozenset({"sudo", "env", "exec", "command", "nohup", "time", "nice"})
# Characters that end a simple command on a line.
_COMMAND_BREAKS = ";&|("
# Stands for a ``$`` that the shell takes as text (in single quotes, or after a backslash),
# so that it is not read as the start of a variable. No command line holds this character.
_LITERAL_DOLLAR = "\0"

# name -> value for variables set on the command line; None for a value that can't be known.
Variables = Mapping[str, "str | None"]


@dataclass(frozen=True)
class HereDocument:
    """A here-document opened on a line: how it ends, and what the shell does with its body."""

    closing_word: str
    # ``<<-``: the shell strips leading tabs, also from the closing line.
    tabs_allowed: bool
    # A closing word with any quoting: the body is passed on as it is, nothing in it runs.
    literal: bool
    # The body goes to a shell (``bash <<EOF``), which runs it as a script.
    feeds_a_shell: bool


def without_wrappers(words: list[str]) -> list[str]:
    """``words`` without leading words that only start another program (``sudo``, ``env X=1``)."""
    index = 0
    while index < len(words) and os.path.basename(words[index]) in _WRAPPERS:
        index += 1
        while index < len(words) and (
            words[index].startswith("-") or _ASSIGNMENT.fullmatch(words[index])
        ):
            index += 1
    return words[index:]


def _is_shell(command_text: str) -> bool:
    """Whether the simple command that ``command_text`` starts is a shell reading its input."""
    _, words = split_assignments(command_text.split())
    words = without_wrappers(words)
    return bool(words) and os.path.basename(words[0]) in _SHELLS


def _here_document(line: str, after_operator: int, command_text: str) -> HereDocument | None:
    """The here-document whose ``<<`` ends at ``after_operator``, or None if it is not one."""
    match = _CLOSING_WORD.match(line, after_operator)
    if match is None:
        return None
    written = match.group(2)
    # The shell removes quoting from the word; the body ends at what is left.
    closing_word = "".join(shlex.split(written))
    literal = closing_word != written
    if not literal and not (closing_word[:1].isalpha() or closing_word[:1] == "_"):
        return None  # ``1 << 4`` is a shift, not a here-document
    return HereDocument(closing_word, bool(match.group(1)), literal, _is_shell(command_text))


def _read_line(line: str, quotes: list[str]) -> tuple[str, list[HereDocument]]:
    """One line of a command: its text without a trailing comment, and the here-documents it opens.

    ``quotes`` is the quoting the line starts in, innermost last ("" outside quotes,
    one entry more for each ``$(`` that is open). It is updated in place, because a
    quoted string can run over several lines. ``<<`` and ``#`` inside quotes are
    text, not an operator and not the start of a comment. In the text that comes
    back, a ``$`` the shell takes as text is marked, so ``expand`` leaves it alone.
    """
    found: list[HereDocument] = []
    command_starts = [0]  # where the simple command being read began, one per open ``$(``
    text = list(line)
    index = 0
    while index < len(line):
        char, quote = line[index], quotes[-1]
        if char == "\\" and quote != "'":
            if line.startswith("$", index + 1):
                text[index + 1] = _LITERAL_DOLLAR
            index += 2
            continue
        if quote == "'":
            if char == "'":
                quotes[-1] = ""
            elif char == "$":
                text[index] = _LITERAL_DOLLAR
        elif line.startswith("$((", index) and (end := line.find("))", index)) != -1:
            index = end + 1  # arithmetic: ``<<`` in it is a shift
        elif line.startswith("$(", index):
            quotes.append("")
            command_starts.append(index + 2)
            index += 1
        elif char == '"':
            quotes[-1] = "" if quote else '"'
        elif quote == "":
            if char == "'":
                quotes[-1] = "'"
            elif char == ")" and len(quotes) > 1:
                quotes.pop()
                if len(command_starts) > 1:
                    command_starts.pop()
            elif char == "#" and (index == 0 or line[index - 1].isspace()):
                return "".join(text[:index]), found  # a comment: the rest is not code
            elif line.startswith("<<<", index):
                index += 2  # a here-string, not a here-document
            elif line.startswith("<<", index):
                document = _here_document(line, index + 2, line[command_starts[-1] : index])
                if document is not None:
                    found.append(document)
            elif char in _COMMAND_BREAKS:
                command_starts[-1] = index + 1
        index += 1
    return "".join(text), found


def _closing_paren(line: str, start: int) -> int:
    """Where the ``$(`` that opens just before ``start`` closes, or the end of the line.

    Quotes are followed, so a ``)`` inside them does not close anything.
    """
    depth, quote, index = 1, "", start
    while index < len(line):
        char = line[index]
        if char == "\\" and quote != "'":
            index += 1
        elif quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return len(line)


def _substitutions(line: str) -> list[str]:
    """The commands inside ``$(...)`` and backticks on one line of a here-document body."""
    found: list[str] = []
    index = 0
    while index < len(line):
        if line[index] == "\\":
            index += 2  # an escaped ``$`` or backtick is text
        elif line.startswith("$(", index) and not line.startswith("$((", index):
            end = _closing_paren(line, index + 2)
            found.append(line[index + 2 : end])
            index = end + 1
        elif line[index] == "`":
            end = line.find("`", index + 1)
            end = len(line) if end == -1 else end
            found.append(line[index + 1 : end])
            index = end + 1
        else:
            index += 1
    return found


def _runnable(body: list[str], document: HereDocument) -> list[str]:
    """What the shell may run as commands from a here-document body."""
    if document.feeds_a_shell:
        return body  # the body is a script for that shell
    if document.literal:
        return []
    # An unquoted here-document is expanded: command substitutions in it run, the
    # text around them is still data.
    return [command for line in body for command in _substitutions(line)]


def strip_heredocs(command: str) -> str:
    """Leave out comments, and here-document text that the shell only passes on as data.

    Without this, a script or a note that merely mentions a commit command would be
    read as one. The line that opens a here-document is kept. So is what the shell
    does run: a body given to a shell (``bash <<EOF``), and the command substitutions
    in an unquoted here-document.
    """
    lines = command.split("\n")
    kept: list[str] = []
    quotes = [""]
    index = 0
    while index < len(lines):
        line, documents = _read_line(lines[index], quotes)
        index += 1
        kept.append(line)
        for document in documents:
            body: list[str] = []
            while index < len(lines):
                text = lines[index]
                index += 1
                if (text.lstrip("\t") if document.tabs_allowed else text) == document.closing_word:
                    break
                body.append(text)
            kept.extend(_runnable(body, document))
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
    if unknown or "$" in expanded or "`" in expanded:
        return None
    return expanded.replace(_LITERAL_DOLLAR, "$")
