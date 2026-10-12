"""``python -m slopfence``: make the import path safe, then run the command line."""

from __future__ import annotations

import os
import sys


def _leave_out_the_current_folder() -> None:
    """Take the folder slopfence was started in off the import path.

    ``python -m`` puts that folder first. A file of the checked project that is named
    like a module slopfence imports later (``random.py``, ``argparse.py``) would then
    be imported, and so run, in its place. Only ``os`` and ``sys`` are used here: both
    are loaded before this file runs, so nothing is imported while the entry is there.

    The entry stays when slopfence itself lives in that folder (a source checkout that
    is not installed), because slopfence could not be imported without it.
    """
    if not sys.path:
        return
    here = os.path.normcase(os.path.realpath(os.getcwd()))
    first = os.path.normcase(os.path.realpath(sys.path[0] or os.curdir))
    package_home = os.path.normcase(os.path.realpath(os.path.dirname(os.path.dirname(__file__))))
    if first == here and package_home != here:
        del sys.path[0]


# Only when ``python -m`` runs this file. A worker process may import this module
# again: it must neither change its import path nor start a run.
if __name__ == "__main__":
    _leave_out_the_current_folder()

from slopfence.cli import main  # noqa: E402  (the import path has to be safe first)

if __name__ == "__main__":
    sys.exit(main())
