from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"low": 1, "medium": 2, "high": 3}[self.value]


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    severity: Severity
    summary: str


# SLOP002: a package counts as new for this many days after its first upload to PyPI.
# The default of ``min-package-age``.
NEW_PACKAGE_DAYS = 30
# SLOP002 never reports a package whose oldest file is this many days old or more,
# unless PyPI has quarantined it. Also the largest ``min-package-age``.
YOUNG_PACKAGE_DAYS = 365

RULES: dict[str, Rule] = {
    rule.id: rule
    for rule in [
        Rule(
            "SLOP001",
            "nonexistent-package",
            Severity.HIGH,
            "Dependency or import that does not exist on PyPI (possible hallucination).",
        ),
        Rule(
            "SLOP002",
            "slopsquatting-risk",
            Severity.HIGH,
            "Dependency or import whose PyPI package is new, has a single release or is "
            "quarantined (slopsquatting risk).",
        ),
        Rule(
            "SLOP010",
            "placeholder-comment",
            Severity.MEDIUM,
            "Placeholder or stub comment left in code that looks finished.",
        ),
        Rule(
            "SLOP011",
            "stub-function",
            Severity.MEDIUM,
            "Function whose docstring promises work but which only returns hardcoded "
            "data or nothing.",
        ),
        Rule(
            "SLOP020",
            "test-asserts-only-mocks",
            Severity.HIGH,
            "Test only asserts on mocks it created itself and never calls real code.",
        ),
        Rule(
            "SLOP021",
            "test-trivial-assertion",
            Severity.MEDIUM,
            "Test whose only assertions are always true (e.g. `assert True`).",
        ),
        Rule(
            "SLOP022",
            "test-cannot-fail",
            Severity.HIGH,
            "Test assertions are wrapped in try/except that swallows the failure.",
        ),
        Rule(
            "SLOP030",
            "duplicate-function",
            Severity.MEDIUM,
            "Function that is identical or nearly identical to another one in the project.",
        ),
        Rule(
            "SLOP040",
            "swallowed-exception",
            Severity.LOW,
            "`except Exception` (or bare `except`) that silently ignores every error.",
        ),
        Rule(
            "SLOP051",
            "chat-leftover",
            Severity.MEDIUM,
            "Leftover AI chat text in a comment or docstring.",
        ),
    ]
}


def plural(count: int, noun: str) -> str:
    """The count with its noun, in the singular for one: "1 finding", "3 findings"."""
    return f"{count} {noun}{'' if count == 1 else 's'}"


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line: int
    col: int
    message: str
    # Last line of the flagged construct, used by --diff to match changed functions.
    end_line: int | None = None
    # What the finding is about: a package, function or test name, or the text of the
    # flagged comment. Baselines match findings on it, so it must stay the same when
    # the code moves: no line number, no other file's location. Every rule sets it.
    subject: str | None = None
    # Set by the engine when the config gives this rule another severity.
    severity_override: Severity | None = None
    # SLOP030 with --diff only: (path, subject) of a copy of this function whose
    # baseline entry also stands for this finding. A full scan never reports the first
    # copy of a group of identical functions, so that copy has no entry of its own
    # (see ``baseline.match``).
    peer: tuple[str, str] | None = None

    @property
    def severity(self) -> Severity:
        """The severity the config sets for the finding's rule, or else the rule's own."""
        return self.severity_override or RULES[self.rule].severity
