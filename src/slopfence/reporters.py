from __future__ import annotations

import json
from collections import Counter
from itertools import groupby
from urllib.parse import quote

from slopfence import __version__
from slopfence.engine import Result
from slopfence.models import RULES, Severity, plural

_SEVERITY_ORDER = [Severity.HIGH, Severity.MEDIUM, Severity.LOW]
_SARIF_LEVEL = {Severity.HIGH: "error", Severity.MEDIUM: "warning", Severity.LOW: "note"}
# Where a SARIF finding links for help. A test keeps it in step with the
# README's headings (tests/test_reporters.py), so editing it alone is not enough.
_HELP_URI = "https://github.com/syedmuhdahmad/slopfence#detectors"
_COLOR = {Severity.HIGH: "\033[31m", Severity.MEDIUM: "\033[33m", Severity.LOW: "\033[36m"}
_RESET, _BOLD, _DIM, _GREEN = "\033[0m", "\033[1m", "\033[2m", "\033[32m"


def text(result: Result, color: bool = False, strict: bool = False) -> str:
    """Human-readable report: findings grouped by file, a summary, then warnings and notes."""

    def paint(s: str, *codes: str) -> str:
        return f"{''.join(codes)}{s}{_RESET}" if color else s

    out: list[str] = []
    for path, group in groupby(result.findings, key=lambda f: f.path):
        out.append(paint(path, _BOLD))
        for f in group:
            tint = _COLOR[f.severity]
            loc = paint(f"{f.line}:{f.col}".ljust(8), _DIM)
            rule = paint(f.rule, _BOLD, tint)
            sev = paint(f.severity.value, tint)
            out.append(f"  {loc} {rule}  {f.message}  [{sev}]")
        out.append("")

    counts = Counter(f.severity for f in result.findings)
    hidden = len(result.baselined)
    if result.findings:
        parts = [paint(f"{counts[s]} {s.value}", _COLOR[s]) for s in _SEVERITY_ORDER if counts[s]]
        summary = paint(plural(len(result.findings), "issue"), _BOLD)
        more = f"; {hidden} more hidden by the baseline" if hidden else ""
        out.append(f"{summary} ({', '.join(parts)}){more}")
    elif hidden:
        out.append(
            paint(
                f"No new issues found in {result.files_checked} files; "
                f"{hidden} hidden by the baseline.",
                _GREEN,
            )
        )
    else:
        out.append(paint(f"No issues found in {result.files_checked} files.", _GREEN))
    label = "error" if strict else "warning"
    for path in result.parse_errors:
        out.append(f"{label}: could not parse {path} (not valid Python), so it was not checked")
    out.extend(f"note: {note}" for note in result.notes)
    return "\n".join(out)


def as_json(result: Result) -> str:
    """Machine-readable report: findings, how many the baseline hid, parse errors, notes."""
    return json.dumps(
        {
            "version": __version__,
            "files_checked": result.files_checked,
            "findings": [
                {
                    "rule": f.rule,
                    "name": RULES[f.rule].name,
                    "severity": f.severity.value,
                    "path": f.path,
                    "line": f.line,
                    "column": f.col,
                    "message": f.message,
                }
                for f in result.findings
            ],
            "baselined": len(result.baselined),
            "parse_errors": result.parse_errors,
            "notes": result.notes,
        },
        indent=2,
    )


def sarif(result: Result, strict: bool = False) -> str:
    """SARIF 2.1.0 report; parse errors and notes become tool notifications.

    Findings that the baseline hid are left out, like in the other reports. A
    notification says how many there were.
    """
    rules = [
        {
            "id": rule.id,
            "name": rule.name,
            "shortDescription": {"text": rule.summary},
            "helpUri": _HELP_URI,
            # The severity this run used: the config can give a rule another one.
            "defaultConfiguration": {
                "level": _SARIF_LEVEL[result.severities.get(rule.id, rule.severity)]
            },
        }
        for rule in RULES.values()
    ]
    rule_index = {rule["id"]: i for i, rule in enumerate(rules)}
    results = [
        {
            "ruleId": f.rule,
            "ruleIndex": rule_index[f.rule],
            "level": _SARIF_LEVEL[f.severity],
            "message": {"text": f.message},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {
                            "uri": quote(f.path, safe="/"),
                            "uriBaseId": "%SRCROOT%",
                        },
                        "region": {"startLine": f.line, "startColumn": f.col},
                    }
                }
            ],
        }
        for f in result.findings
    ]
    notes = list(result.notes)
    if result.baselined:
        notes.append(f"{plural(len(result.baselined), 'finding')} hidden by the baseline")
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "slopfence",
                        "version": __version__,
                        "informationUri": "https://github.com/syedmuhdahmad/slopfence",
                        "rules": rules,
                    }
                },
                "invocations": [
                    {
                        "executionSuccessful": not (strict and result.parse_errors),
                        "toolExecutionNotifications": [
                            {
                                "level": "error" if strict else "warning",
                                "message": {
                                    "text": f"Could not parse {path} (not valid Python), "
                                    "so it was not checked"
                                },
                                "locations": [
                                    {
                                        "physicalLocation": {
                                            "artifactLocation": {
                                                "uri": quote(path, safe="/"),
                                                "uriBaseId": "%SRCROOT%",
                                            }
                                        }
                                    }
                                ],
                            }
                            for path in result.parse_errors
                        ]
                        + [{"level": "note", "message": {"text": note}} for note in notes],
                    }
                ],
                "results": results,
            }
        ],
    }
    return json.dumps(doc, indent=2)
