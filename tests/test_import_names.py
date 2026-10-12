"""Import names that differ from their distribution's name (#16)."""

import json

import pytest

from slopfence.detectors.import_names import (
    IMPORT_NAMES,
    STDLIB_ON_OTHER_PYTHONS,
    is_stdlib,
    provides,
)
from slopfence.detectors.packages import parse_lock_file
from tests.conftest import FakeRegistry, rule_lines


@pytest.mark.parametrize(
    ("distribution", "module"),
    [
        # Listed in IMPORT_NAMES: names with nothing in common.
        ("beautifulsoup4", "bs4"),
        ("scikit-learn", "sklearn"),
        ("opencv-python-headless", "cv2"),
        ("Pillow", "PIL"),
        ("attrs", "attr"),
        ("djangorestframework", "rest_framework"),
        ("google-api-python-client", "googleapiclient"),
        # By rule: prefixes, suffixes, separators and namespace packages.
        ("python-dateutil", "dateutil"),
        ("PyYAML", "yaml"),
        ("PyGithub", "github"),
        ("msgpack-python", "msgpack"),
        ("dnspython", "dns"),
        ("SpeechRecognition", "speech_recognition"),
        ("Flask-SQLAlchemy", "flask_sqlalchemy"),
        ("google-cloud-storage", "google"),
        ("azure-storage-blob", "azure"),
        ("pdfminer.six", "pdfminer"),
        ("ruamel.yaml", "ruamel"),
        ("nats-py", "nats"),
    ],
)
def test_provides_real_examples(distribution, module):
    """Real distributions and the name they are imported under."""
    assert provides(distribution, module)


@pytest.mark.parametrize(
    ("distribution", "module"),
    [
        ("requests", "flask"),
        ("numpy", "pandas"),
        ("google-cloud-storage", "goo"),
        ("django", "rest_framework"),
        ("pyyaml", "toml"),
        ("flask-jwt-simple-auth", "jwt_simple"),
    ],
)
def test_provides_rejects_unrelated_names(distribution, module):
    """A declared distribution doesn't vouch for an unrelated import."""
    assert not provides(distribution, module)


def test_mapping_values_are_normalized():
    """Mapping values are compared with normalized names, so they must be normalized."""
    from slopfence.registry import normalize

    for module, dists in IMPORT_NAMES.items():
        assert dists, module
        assert all(d == normalize(d) for d in dists), module


def test_declared_but_not_installed_imports_are_not_flagged(write, check, not_installed):
    """No false SLOP001 for declared packages imported under a different name."""
    write(
        "requirements.txt",
        "beautifulsoup4\npython-dateutil\nscikit-learn\ngoogle-cloud-storage\nPyGithub\n",
    )
    write(
        "src/app.py",
        """
        import bs4
        import dateutil.parser
        from sklearn.linear_model import LinearRegression
        from google.cloud import storage
        from github import Github
        import fastjsonvalidatorx
        """,
    )
    declared = {"beautifulsoup4", "python-dateutil", "scikit-learn", "google-cloud-storage"}
    registry = FakeRegistry(declared | {"PyGithub"})
    result = check("SLOP001", registry=registry, check_imports=True)
    assert rule_lines(result, "SLOP001") == [6]
    # Besides the declared dependencies, only the invented import was looked up.
    assert set(registry.queries) - declared - {"PyGithub"} == {"fastjsonvalidatorx"}


# Standard library modules that some supported Python lacks: removed in 3.12 or 3.13,
# removed before 3.10, or added in 3.11 or 3.14.
OTHER_PYTHONS = [
    "imp", "asyncore", "asynchat", "smtpd", "distutils", "lib2to3", "cgi", "cgitb",
    "telnetlib", "pipes", "crypt", "formatter", "parser", "symbol", "macpath",
    "tomllib", "compression", "annotationlib",
]  # fmt: skip


def test_the_standard_library_of_every_python_counts():
    """``is_stdlib`` knows the modules of the running Python and those that another
    Python 3 has or had. A package and an invented name are neither."""
    assert all(is_stdlib(module) for module in OTHER_PYTHONS)
    assert set(OTHER_PYTHONS) <= STDLIB_ON_OTHER_PYTHONS
    assert all(is_stdlib(module) for module in ("json", "sys", "__future__", "os"))
    assert not any(is_stdlib(module) for module in ("requests", "ghost_mod_zz", "Imp"))


def test_a_module_of_another_pythons_standard_library_is_never_looked_up(
    write, check, not_installed
):
    """``import imp`` on Python 3.14 and ``import tomllib`` on Python 3.10 are no
    invented packages. A Python file and a notebook get the same report on whichever
    Python slopfence runs: only the invented import is sent and reported."""
    code = "".join(f"import {module}\n" for module in OTHER_PYTHONS) + "import ghost_mod_zz\n"
    write("old.py", code)
    cell = {"cell_type": "code", "source": code}
    write("old.ipynb", json.dumps({"cells": [cell], "metadata": {}, "nbformat": 4}))
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, check_imports=True)
    assert [(f.path, f.subject) for f in result.findings] == [
        ("old.ipynb", "ghost_mod_zz"),
        ("old.py", "ghost_mod_zz"),
    ]
    assert set(registry.queries) == {"ghost-mod-zz", "ghost_mod_zz"}


LOCKS = {
    "uv.lock": 'version = 1\n\n[[package]]\nname = "idna"\nversion = "3.7"\n',
    "poetry.lock": '[[package]]\nname = "charset-normalizer"\nversion = "3.3.2"\n',
    "pdm.lock": '[[package]]\nname = "markdown-it-py"\nversion = "3.0.0"\n',
    "Pipfile.lock": json.dumps(
        {"_meta": {"sources": []}, "default": {"certifi": {}}, "develop": {"pluggy": {}}}
    ),
}


@pytest.mark.parametrize("lock", sorted(LOCKS))
def test_lock_files_vouch_for_indirect_imports(write, check, not_installed, lock):
    """Packages in lock files (including indirect ones) count as installed by the project."""
    names, private = parse_lock_file(write(lock, LOCKS[lock]))
    assert names and not private
    module = {
        "uv.lock": "idna",
        "poetry.lock": "charset_normalizer",
        "pdm.lock": "markdown_it",
        "Pipfile.lock": "certifi",
    }[lock]
    write("app.py", f"import {module}\nimport ghost_pkg_zz\n")
    registry = FakeRegistry(set())
    result = check("SLOP001", registry=registry, check_imports=True)
    assert rule_lines(result, "SLOP001") == [2]
    assert module not in registry.queries


def test_broken_lock_file_is_ignored(write, tmp_path):
    """An unreadable lock file contributes no name instead of crashing the run. Of a
    ``Pipfile.lock``, which records its indexes, nobody can then say whether it uses
    a private one."""
    assert parse_lock_file(write("uv.lock", "[[package\n")) == (set(), False)
    assert parse_lock_file(write("Pipfile.lock", "{not json")) == (set(), None)
