# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the landing TLC receipt publish and sync commands."""

from .handlers.handler_landing_tlc_receipt_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
