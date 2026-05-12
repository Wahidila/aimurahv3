#!/usr/bin/env python3
"""Compatibility launcher. Prefer `./aimurahv3 start` or `python -m aimurah start`."""
import sys

from aimurah.cli import main

# Default to `start --foreground` so `python run.py` behaves like before.
argv = sys.argv[1:] or ["start", "--foreground"]
sys.exit(main(argv))
