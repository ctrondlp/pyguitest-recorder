"""Allow `python -m pyguitest_recorder` alongside the installed script."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
