"""Project configuration from ``[tool.slopfence]`` in ``pyproject.toml``."""

from __future__ import annotations

import fnmatch
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from slopfence.models import NEW_PACKAGE_DAYS, RULES, YOUNG_PACKAGE_DAYS, Severity

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

KNOWN_KEYS = {
    "select",
    "ignore",
    "exclude",
    "known-packages",
    "min-package-age",
    "check-imports",
    "detect-private-index",
    "fail-on",
    "jobs",
    "strict",
    "per-file-ignores",
    "severity",
    "baseline",
}
FAIL_ON = [s.value for s in Severity]  # high, medium, low
# What a ``min-package-age`` must be. A year is the most: SLOP002 never reports a
# package that is older.
PACKAGE_AGE = f"a whole number of days from 1 to {YOUNG_PACKAGE_DAYS}"


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    """The settings of ``[tool.slopfence]``, with their defaults."""

    select: list[str] | None = None
    ignore: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    known_packages: list[str] = field(default_factory=list)
    # SLOP002 reports a package whose oldest file on PyPI is younger than this many days.
    min_package_age: int = NEW_PACKAGE_DAYS
    check_imports: bool = False
    detect_private_index: bool = True
    fail_on: str = "low"
    strict: bool = False
    # Worker processes for parsing and checking files. None: one per CPU, at most 8.
    jobs: int | None = None
    # Pattern (same rules as ``exclude``) -> the rules switched off in matching files.
    per_file_ignores: dict[str, list[str]] = field(default_factory=dict)
    # Rule ID -> the severity that replaces the rule's own.
    severity: dict[str, Severity] = field(default_factory=dict)
    # The baseline file, as written in the config: relative to the project root.
    baseline: str | None = None
    path: Path | None = None


def _str_list(table: dict, key: str, where: str) -> list[str]:
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}: '{key}' must be a list of strings")
    return value


def _bool(table: dict, key: str, where: str) -> bool:
    """Return ``table[key]``, which must be a TOML boolean."""
    value = table[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: '{key}' must be true or false")
    return value


def is_package_age(value: object) -> bool:
    """Whether ``value`` can be a ``min-package-age`` (see ``PACKAGE_AGE``)."""
    # ``type``, not ``isinstance``: True is an int too.
    return type(value) is int and 1 <= value <= YOUNG_PACKAGE_DAYS


def _rule_ids(values: list[str], key: str, where: str) -> list[str]:
    rules = [v.strip().upper() for v in values]
    unknown = [r for r in rules if r not in RULES]
    if unknown:
        raise ConfigError(f"{where}: unknown rule(s) in '{key}': {', '.join(unknown)}")
    return rules


def _clean_pattern(raw: str) -> str:
    """An exclude-style pattern without spaces around it, a leading "./" or a trailing "/"."""
    return raw.strip().removeprefix("./").rstrip("/")


def _per_file_ignores(table: dict, where: str) -> dict[str, list[str]]:
    """Return ``table["per-file-ignores"]``: a table of pattern = [rule IDs]."""
    value = table["per-file-ignores"]
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: 'per-file-ignores' must be a table of pattern = [rule IDs]")
    ignores: dict[str, list[str]] = {}
    for pattern, rules in value.items():
        if not _clean_pattern(pattern):
            raise ConfigError(f"{where}: 'per-file-ignores' has an empty pattern")
        if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
            # TOML reads an unquoted key like conftest.py as a table inside a table.
            hint = (
                " (put quotes around a pattern that contains a dot)"
                if isinstance(rules, dict)
                else ""
            )
            raise ConfigError(
                f"{where}: 'per-file-ignores': the value for \"{pattern}\" must be a list "
                f"of rule IDs{hint}"
            )
        ignores[pattern] = _rule_ids(rules, f'per-file-ignores."{pattern}"', where)
    return ignores


def _severities(table: dict, where: str) -> dict[str, Severity]:
    """Return ``table["severity"]``: a table of rule ID = "high", "medium" or "low"."""
    value = table["severity"]
    if not isinstance(value, dict):
        raise ConfigError(
            f'{where}: \'severity\' must be a table of rule ID = "high", "medium" or "low"'
        )
    severities: dict[str, Severity] = {}
    for key, level in value.items():
        [rule] = _rule_ids([key], "severity", where)
        if rule in severities:
            raise ConfigError(f"{where}: 'severity' sets {rule} more than once")
        if not isinstance(level, str) or level not in FAIL_ON:
            raise ConfigError(f"{where}: 'severity': {rule} must be one of {', '.join(FAIL_ON)}")
        severities[rule] = Severity(level)
    return severities


def load_config(root: Path) -> Config:
    """Read [tool.slopfence] from root/pyproject.toml. Missing file or table -> defaults."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as err:
        raise ConfigError(f"{path}: {err}") from err
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"{path}: invalid TOML: {err}") from err

    table = data.get("tool", {}).get("slopfence")
    if table is None:
        return Config()
    where = f"{path} [tool.slopfence]"
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    unknown = sorted(set(table) - KNOWN_KEYS)
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s): {', '.join(unknown)} "
            f"(supported: {', '.join(sorted(KNOWN_KEYS))})"
        )

    config = Config(path=path)
    if "select" in table:
        config.select = _rule_ids(_str_list(table, "select", where), "select", where)
        if not config.select:
            raise ConfigError(f"{where}: 'select' must not be empty (omit it to run all rules)")
    if "ignore" in table:
        config.ignore = _rule_ids(_str_list(table, "ignore", where), "ignore", where)
    if "exclude" in table:
        config.exclude = _str_list(table, "exclude", where)
    if "known-packages" in table:
        config.known_packages = _str_list(table, "known-packages", where)
    if "min-package-age" in table:
        value = table["min-package-age"]
        if not is_package_age(value):
            raise ConfigError(f"{where}: 'min-package-age' must be {PACKAGE_AGE}")
        config.min_package_age = value
    if "fail-on" in table:
        value = table["fail-on"]
        if value not in FAIL_ON:
            raise ConfigError(f"{where}: 'fail-on' must be one of {', '.join(FAIL_ON)}")
        config.fail_on = value
    if "check-imports" in table:
        config.check_imports = _bool(table, "check-imports", where)
    if "detect-private-index" in table:
        config.detect_private_index = _bool(table, "detect-private-index", where)
    if "strict" in table:
        config.strict = _bool(table, "strict", where)
    if "jobs" in table:
        value = table["jobs"]
        # bool is a kind of int in Python, so "jobs = true" has to be turned down by hand.
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(f"{where}: 'jobs' must be a whole number, 1 or more")
        config.jobs = value
    if "per-file-ignores" in table:
        config.per_file_ignores = _per_file_ignores(table, where)
    if "severity" in table:
        config.severity = _severities(table, where)
    if "baseline" in table:
        value = table["baseline"]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{where}: 'baseline' must be a file path")
        config.baseline = value
    return config


def slop002_has_no_lookups(select: Iterable[str] | None, ignore: Iterable[str]) -> bool:
    """Whether SLOP002 is not ignored and still must not run, because SLOP001 is ignored.

    SLOP002 reads the answers to SLOP001's PyPI lookups and sends no name itself. A
    project that ignores SLOP001 sends nothing to PyPI, and a new version must not
    change that. Naming SLOP002 in ``select`` is how to ask for the lookups without
    SLOP001. A ``select`` list that doesn't name it leaves it out anyway.
    """
    ignored = set(ignore)
    return not select and "SLOP001" in ignored and "SLOP002" not in ignored


def selected_rules(select: Iterable[str] | None, ignore: Iterable[str]) -> set[str]:
    """The rules that run: the ``select`` list, or every rule, without the ``ignore`` list.

    SLOP002 is left out too where it has no lookups to read (see ``slop002_has_no_lookups``).
    """
    ignored = set(ignore)  # read once: it may be an iterator
    rules = (set(select) if select else set(RULES)) - ignored
    return rules - {"SLOP002"} if slop002_has_no_lookups(select, ignored) else rules


def ignored_rules(rel_path: str, per_file_ignores: Mapping[str, Iterable[str]]) -> frozenset[str]:
    """The rules that ``per-file-ignores`` switches off for a project-relative POSIX path.

    A pattern works like an ``exclude`` pattern (see ``is_excluded``). When several
    patterns match the file, their rule lists add up.
    """
    return frozenset(
        rule
        for pattern, rules in per_file_ignores.items()
        if is_excluded(rel_path, [pattern])
        for rule in rules
    )


def is_excluded(rel_path: str, patterns: Iterable[str]) -> bool:
    """Whether a project-relative POSIX path matches any exclude pattern.

    Like .gitignore: a pattern without "/" (a trailing "/" doesn't count) matches
    any path component ("migrations", "migrations/", "*_pb2.py"). A pattern with
    "/" in the middle is anchored at the project root and matches the path or any
    of its parent directories ("tests/fixtures/", "src/gen/*.py").
    """
    parts = rel_path.split("/")
    for raw in patterns:
        pattern = _clean_pattern(raw)
        if not pattern:
            continue
        if "/" in pattern:
            candidates = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
        else:
            candidates = parts
        if any(fnmatch.fnmatchcase(c, pattern) for c in candidates):
            return True
    return False
