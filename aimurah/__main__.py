"""Module entry: `python -m aimurah ...` dispatches to the CLI."""
from .cli import main
import sys

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
