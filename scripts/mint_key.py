#!/usr/bin/env python3
"""Thin wrapper around `hrserv.scripts.mint_key`.

The actual implementation lives in the installed package so it can be unit-tested
and so `uv run hrserv-mint-key` (the console-script entry point in
`pyproject.toml`) and `python scripts/mint_key.py` invoke the same code path.
"""

from __future__ import annotations

import sys

from hrserv.scripts.mint_key import main

if __name__ == "__main__":
    sys.exit(main())
