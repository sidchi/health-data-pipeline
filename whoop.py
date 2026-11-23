#!/usr/bin/env python3
"""Local WHOOP exporter. See README.md for setup.

python whoop.py [--days N] [--out DIR] [--no-drive]
"""

import sys

from whoop_exporter.cli import main

if __name__ == "__main__":
    sys.exit(main())
