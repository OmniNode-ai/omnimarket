# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for the contract-bound retention cleanup handler."""

from omnimarket.nodes.node_retention_cleanup.runtime_retention_cleanup import (
    main,
)

if __name__ == "__main__":
    main()
