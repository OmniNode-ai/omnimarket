# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stands in for onex-ledger in tests: ``ledger_append_stub.py <ledger> --append <row>``."""

import sys
from pathlib import Path

if __name__ == "__main__":
    ledger, flag, row = sys.argv[1:4]
    if flag != "--append" or "REFUSE" in row:
        sys.stderr.write("refused\n")
        sys.exit(1)
    with Path(ledger).open("a", encoding="utf-8") as handle:
        handle.write(row + "\n")
