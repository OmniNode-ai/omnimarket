# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Entry point for the contract-driven retention cleanup node."""

from .handlers.handler_retention_cli import main as main

if __name__ == "__main__":
    main()
