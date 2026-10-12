import sys

from slopfence.cli import main

# Guarded: a worker process may import this module again, and must not run it.
if __name__ == "__main__":
    sys.exit(main())
