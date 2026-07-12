"""Allow running the CLI as ``python -m pptrepair``."""

import sys

from pptrepair.cli import main

if __name__ == "__main__":
    sys.exit(main())
