#!/usr/bin/env python3
"""Repo-local request wrapper for the OmniMarket Codex runtime client."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _main() -> int:
    from omnimarket.adapters.codex.runtime_client import main

    return main()


if __name__ == "__main__":
    raise SystemExit(_main())
