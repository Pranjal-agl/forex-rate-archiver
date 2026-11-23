"""Backward-compatible entry point. Prefer: ``forex-archiver run`` (after ``pip install .``)."""

import sys

from forex_archiver.cli import main

if __name__ == "__main__":
    sys.exit(main(["run", *sys.argv[1:]]))
