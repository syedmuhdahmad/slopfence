"""``pip install`` command lines: which packages they name (SLOP001).

A notebook installs its packages with shell lines (``!pip install x``) and magics
(``%pip install x``). This module reads such a line as text. Nothing is run and no
variable is expanded: what can't be read with certainty gives no name to look up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import NamedTuple

from slopfence.detectors.dependency import (
    INDEX_OPTION_STARTS,
    LINKS_OPTION_STARTS,
    egg_name,
    is_pip_option,
    parse_requirement,
)
from slopfence.indexes import is_public_index, named_index_url


class Word(NamedTuple):
    """One word of a shell command, without its quotes."""

    text: str
    # Where the word starts and ends in the text that was split.
    start: int
    end: int


_PIP = re.compile(r"pip[0-9.]*$")
# What a double-quoted shell string lets a backslash escape. Any other backslash stays.
_DOUBLE_QUOTE_ESCAPE = re.compile(r'\\(["\\$`])')
# A word that still holds one of these was not a fixed string for the shell: "$PKG",
# "{package}" (IPython fills it in), "`cmd`", and "%PKG%" on Windows.
_UNEXPANDED = re.compile(r"[${`]|%\w+%")

# ``pip install`` and ``uv pip install`` options that take no value.
PIP_FLAGS = frozenset(
    {
        "--all-extras", "--break-system-packages", "--check-build-dependencies", "--compile",
        "--compile-bytecode", "--debug", "--disable-pip-version-check", "--dry-run", "--exact",
        "--force-reinstall", "--help", "--ignore-installed", "--ignore-requires-python",
        "--isolated", "--native-tls", "--no-build", "--no-build-isolation", "--no-cache",
        "--no-cache-dir", "--no-clean", "--no-color", "--no-compile", "--no-config",
        "--no-dependencies", "--no-deps", "--no-index", "--no-input", "--no-progress",
        "--no-python-version-warning", "--no-sources", "--no-use-pep517", "--no-user",
        "--no-warn-conflicts", "--no-warn-script-location", "--offline", "--pre",
        "--prefer-binary", "--process-dependency-links", "--quiet", "--refresh", "--reinstall",
        "--require-hashes", "--require-venv", "--require-virtualenv", "--strict", "--system",
        "--upgrade", "--use-pep517", "--user", "--verbose", "--version", "-I", "-U", "-V", "-h",
        "-n", "-q", "-v",
    }
)  # fmt: skip
# Options that take a value (the next word, or after "="). Where pip and uv disagree
# about an option, it is listed here: a word too many is dropped, never one too few.
PIP_VALUE_OPTIONS = frozenset(
    {
        "--abi", "--allow-insecure-host", "--build", "--build-constraint", "--build-constraints",
        "--cache-dir", "--cert", "--client-cert", "--color", "--config-file", "--config-setting",
        "--config-settings", "--constraint", "--constraints", "--default-index",
        "--default-timeout", "--directory", "--editable", "--exclude-newer", "--exists-action",
        "--extra", "--extra-index-url", "--find-links", "--global-option", "--group",
        "--implementation", "--index", "--index-strategy", "--index-url", "--install-option",
        "--keyring-provider", "--link-mode", "--local-log", "--log", "--log-file", "--no-binary",
        "--only-binary", "--overrides", "--platform", "--prefix", "--prerelease",
        "--progress-bar", "--project", "--proxy", "--pypi-url", "--python", "--python-platform",
        "--python-version", "--refresh-package", "--reinstall-package", "--report",
        "--requirement", "--requirements", "--resolution", "--resume-retries", "--retries",
        "--root", "--root-user-action", "--source", "--source-dir", "--source-directory", "--src",
        "--target", "--timeout", "--torch-backend", "--trusted-host", "--upgrade-package",
        "--upgrade-strategy", "--use-deprecated", "--use-feature", "-C", "-P", "-b", "-c", "-e",
        "-f", "-i", "-p", "-r", "-t",
    }
)  # fmt: skip
# Options that name a package index: one that isn't PyPI may serve any package.
INDEX_OPTIONS = frozenset(
    {"-i", "--index-url", "--extra-index-url", "--pypi-url", "--index", "--default-index"}
)
# Options that add files or pages to install from, whatever their value is.
LINK_OPTIONS = frozenset({"-f", "--find-links", "--no-index", "--process-dependency-links"})
# Options that install what a file or a folder lists. Its packages have no name here.
UNNAMED_OPTIONS = frozenset(
    {"-r", "--requirement", "--requirements", "-e", "--editable", "--group"}
)


def shell_commands(text: str, i: int) -> tuple[list[list[Word]], int]:
    """Split one shell line into simple commands, each a list of words.

    Starts at ``text[i]`` and stops at the first line break that isn't escaped
    with a backslash; returns that offset too. Quotes are removed, ``;``, ``&``,
    ``|`` and brackets start a new command, a comment ends the line, and everything
    after a redirection (``>``, ``<``) is dropped from its command. A backslash in
    front of another character stays in the word: such a word is a Windows path or
    something escaped, never a package name. An unterminated quote gives no commands.
    """
    commands: list[list[Word]] = [[]]
    chars: list[str] | None = None
    start = 0
    redirected = False
    n = len(text)

    def flush() -> None:
        """End the current word."""
        nonlocal chars
        if chars is not None and not redirected:
            commands[-1].append(Word("".join(chars), start, i))
        chars = None

    while i < n and text[i] != "\n":
        ch = text[i]
        if ch in " \t\r":
            flush()
        elif ch == "#" and chars is None:
            end = text.find("\n", i)
            i = n if end == -1 else end
            break
        elif ch in ";&|()":
            flush()
            commands.append([])
            redirected = False
        elif ch in "<>":
            if chars is not None and "".join(chars).isdigit():
                chars = None  # "2>" is a file descriptor, not a word
            flush()
            redirected = True
        else:
            if chars is None:
                chars, start = [], i
            if ch == "\\":
                i += 1
                if i < n and text[i] != "\n":
                    chars.append("\\" + text[i])
                elif not chars:
                    chars = None  # a line continuation between words
            elif ch in "'\"":
                end = i + 1
                while end < n and text[end] != ch:
                    end += 2 if ch == '"' and text[end] == "\\" else 1
                if end >= n:
                    return [], n  # unterminated quote: don't guess
                body = text[i + 1 : end]
                chars.append(_DOUBLE_QUOTE_ESCAPE.sub(r"\1", body) if ch == '"' else body)
                i = end
            else:
                chars.append(ch)
        i += 1
    flush()
    return commands, min(i, n)


def pip_install_arguments(words: list[Word]) -> list[Word] | None:
    """The words after ``pip install`` if a command is one, else None.

    Accepts ``pip``, ``pip3``, ``pip3.12``, ``<python> -m pip`` and ``uv pip``.
    """
    names = [word.text for word in words]
    at = 0
    if len(names) > 2 and names[1] == "-m":
        at = 2
    elif names[:1] == ["uv"]:
        at = 1
    if len(names) > at + 1 and _PIP.match(names[at]) and names[at + 1] == "install":
        return words[at + 2 :]
    return None


@dataclass
class PipInstall:
    """What one ``pip install`` command says."""

    # (name, the word that names it, whether that word says where the package comes
    # from: a URL).
    packages: list[tuple[str, Word, bool]] = field(default_factory=list)
    # A word still holds a variable (``$PKG``, ``{pkg}``). It may hold anything, an
    # index option too, so none of the command's names is looked up.
    variables: bool = False
    # The command names a package index that isn't PyPI.
    private_index: bool = False
    # The command adds links to install from (--find-links), or uses no index at all.
    links: bool = False
    # (start, end) of each index or links option, with its value where it was read.
    # What these words say about package sources is in the two fields above.
    source_options: list[tuple[int, int]] = field(default_factory=list)
    # The command also installs something that has no name here: a path, a URL, the
    # packages of a requirements file, what a variable holds. Or it was not read.
    unnamed: bool = False


def _short_options(cluster: str) -> tuple[str, str | None] | None:
    """Read a cluster of short options: ``-qU``, ``-qqq``, ``-rrequirements.txt``.

    Returns the option that takes a value, if the cluster has one, and the value that
    is written right after it (None when it is the next word). ("", None) for a
    cluster of flags only. None when a letter is not an option this module knows.
    """
    for at, letter in enumerate(cluster[1:], start=1):
        option = "-" + letter
        if option in PIP_VALUE_OPTIONS:
            return option, cluster[at + 1 :] or None
        if option not in PIP_FLAGS:
            return None
    return "", None


def _package(word: Word) -> tuple[str, Word, bool] | None:
    """The package a word of a ``pip install`` command names, or None if it names none.

    A URL names one when it says so (``name @ https://...``, ``#egg=name``): that
    package is declared, but it doesn't come from an index. Paths, archives and
    anything else that is not a requirement name nothing.
    """
    text = word.text
    if "://" in text or text.startswith(("git+", "hg+", "svn+", "bzr+")):
        parsed = parse_requirement(text)  # "name @ https://..." in one quoted word
        name = parsed.name if parsed else egg_name(text)
        return (name, word, True) if name else None
    if "/" in text or text.startswith((".", "~")):
        return None  # a local path
    parsed = parse_requirement(text)
    return (parsed.name, word, parsed.direct) if parsed else None


def _unread(arguments: list[Word]) -> PipInstall:
    """What is known of a command that has an option this module doesn't know.

    None of its packages: the value of the unknown option could be taken for one.
    An index or links option among its words still says that packages may come
    from another place than PyPI, whatever its value is.

    A long option that pip lets a user cut short (``--extra-index``) is not in this
    module's tables, so it arrives here. It counts as the option it is short for. A
    command line has more options than a requirements file, so pip refuses a few of
    these short forms as ambiguous (``--ex``): counting them too only keeps more
    names on the machine.
    """
    found = PipInstall(unnamed=True)
    for word in arguments:
        option = word.text.partition("=")[0]
        index = option in INDEX_OPTIONS or is_pip_option(word.text, "-i", INDEX_OPTION_STARTS)
        links = option in LINK_OPTIONS or is_pip_option(word.text, "-f", LINKS_OPTION_STARTS)
        found.private_index = found.private_index or index
        found.links = found.links or links
        if index or links:
            found.source_options.append((word.start, word.end))
    return found


def pip_requirements(arguments: list[Word]) -> PipInstall:
    """What a ``pip install`` command names, from the words after ``install``.

    A command with an option this function doesn't know names no package here (see
    ``_unread``).
    """
    found = PipInstall()
    words = iter(arguments)
    only_names = False
    for word in words:
        text = word.text
        if only_names or not text.startswith("-") or text == "-":
            if package := _package(word):
                found.packages.append(package)
            else:
                found.unnamed = True
            continue
        if text == "--":
            only_names = True
            continue
        option, has_value, value = text.partition("=")
        if not option.startswith("--") and len(option) > 2:
            short = _short_options(option)
            if short is None:
                return _unread(arguments)
            option, attached = short
            has_value, value = ("=", attached) if attached else ("", "")
        last = word
        if option in PIP_VALUE_OPTIONS:
            if not has_value:
                last = next(words, word)
                value = last.text if last is not word else ""
            if option in INDEX_OPTIONS and not is_public_index(named_index_url(value)):
                found.private_index = True
        elif option and option not in PIP_FLAGS:
            return _unread(arguments)
        if option in INDEX_OPTIONS or option in LINK_OPTIONS:
            found.links = found.links or option in LINK_OPTIONS
            found.source_options.append((word.start, last.end))
        found.unnamed = found.unnamed or option in UNNAMED_OPTIONS
    found.variables = any(_UNEXPANDED.search(word.text) for word in arguments)
    found.unnamed = found.unnamed or found.variables
    return found
