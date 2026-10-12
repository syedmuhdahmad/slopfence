import json
import re

from slopfence import reporters
from slopfence.engine import Result
from slopfence.models import Finding

ANSI = re.compile(r"\x1b\[[0-9;]*m")
RESET, BOLD, DIM, GREEN = "\x1b[0m", "\x1b[1m", "\x1b[2m", "\x1b[32m"
RED, YELLOW = "\x1b[31m", "\x1b[33m"


def _result():
    return Result(
        findings=[
            Finding("SLOP001", "requirements.txt", 3, 1, "Dependency 'x' does not exist"),
            Finding("SLOP051", "src/app.py", 12, 5, "Leftover AI chat text"),
        ],
        files_checked=2,
    )


PLAIN = """requirements.txt
  3:1      SLOP001  Dependency 'x' does not exist  [high]

src/app.py
  12:5     SLOP051  Leftover AI chat text  [medium]

2 issues (1 high, 1 medium)"""


def test_plain_output_exact():
    assert reporters.text(_result()) == PLAIN


def test_colour_output_exact_sequences():
    lines = reporters.text(_result(), color=True).splitlines()
    assert lines[0] == f"{BOLD}requirements.txt{RESET}"
    # Location padded to 8 columns *inside* the dim span; rule ID bold + severity colour.
    assert lines[1] == (
        f"  {DIM}3:1     {RESET} {BOLD}{RED}SLOP001{RESET}  Dependency 'x' does not exist"
        f"  [{RED}high{RESET}]"
    )
    assert lines[4] == (
        f"  {DIM}12:5    {RESET} {BOLD}{YELLOW}SLOP051{RESET}  Leftover AI chat text"
        f"  [{YELLOW}medium{RESET}]"
    )
    assert lines[-1] == f"{BOLD}2 issues{RESET} ({RED}1 high{RESET}, {YELLOW}1 medium{RESET})"


def test_colour_output_matches_plain_when_stripped():
    assert ANSI.sub("", reporters.text(_result(), color=True)) == PLAIN


def test_no_colour_leaks_past_a_line():
    # After the last reset on a line, no style may be switched on again,
    # otherwise colour would bleed into the next line or the user's prompt.
    for line in reporters.text(_result(), color=True).splitlines():
        if ANSI.search(line):
            tail = line[line.rfind(RESET) + len(RESET) :]
            assert not ANSI.search(tail), repr(line)


def test_empty_result_is_green_and_plain():
    empty = Result(files_checked=3)
    assert reporters.text(empty) == "No issues found in 3 files."
    assert reporters.text(empty, color=True) == f"{GREEN}No issues found in 3 files.{RESET}"


def test_parse_errors_are_not_coloured():
    result = Result(files_checked=1, parse_errors=["broken.py"])
    expected = "error: could not parse broken.py (not valid Python), so it was not checked"
    assert reporters.text(result, color=True, strict=True).splitlines()[-1] == expected


def test_summary_says_how_many_findings_the_baseline_hid():
    """The summary line counts the baselined findings; colour ends before that part."""
    hidden = [Finding("SLOP040", "src/app.py", 3, 5, "old")] * 3
    result = _result()
    result.baselined = hidden
    assert reporters.text(result).splitlines()[-1] == (
        "2 issues (1 high, 1 medium); 3 more hidden by the baseline"
    )
    assert reporters.text(result, color=True).splitlines()[-1] == (
        f"{BOLD}2 issues{RESET} ({RED}1 high{RESET}, {YELLOW}1 medium{RESET})"
        "; 3 more hidden by the baseline"
    )
    empty = Result(files_checked=3, baselined=hidden[:1])
    assert reporters.text(empty) == "No new issues found in 3 files; 1 hidden by the baseline."
    assert reporters.text(empty, color=True) == (
        f"{GREEN}No new issues found in 3 files; 1 hidden by the baseline.{RESET}"
    )


def test_one_issue_is_singular():
    """The summary says "1 issue", not "1 issues"."""
    result = _result()
    result.findings = result.findings[:1]
    assert reporters.text(result).splitlines()[-1] == "1 issue (1 high)"


def test_json_report_counts_baselined_findings():
    """JSON has the count, between the findings and the parse errors, also when it is 0."""
    result = _result()
    report = json.loads(reporters.as_json(result))
    assert list(report) == [
        "version",
        "files_checked",
        "findings",
        "baselined",
        "parse_errors",
        "notes",
    ]
    assert report["baselined"] == 0
    result.baselined = [Finding("SLOP040", "src/app.py", 3, 5, "old")]
    report = json.loads(reporters.as_json(result))
    assert (report["baselined"], len(report["findings"])) == (1, 2)


def test_sarif_says_one_finding_in_the_singular():
    """The SARIF notification counts the hidden findings after the run's own notes."""
    result = _result()
    result.notes = ["a note of the run"]
    result.baselined = [Finding("SLOP040", "src/app.py", 3, 5, "old")]
    run = json.loads(reporters.sarif(result))["runs"][0]
    notes = [n["message"]["text"] for n in run["invocations"][0]["toolExecutionNotifications"]]
    assert notes == ["a note of the run", "1 finding hidden by the baseline"]
    assert len(run["results"]) == 2
