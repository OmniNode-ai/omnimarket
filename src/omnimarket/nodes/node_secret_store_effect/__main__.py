# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for the secret store effect.

The command line lives in ``runtime_secret_store_cli``; this module only
delegates so ``python -m omnimarket.nodes.node_secret_store_effect`` works.
"""

from __future__ import annotations

from omnimarket.nodes.node_secret_store_effect.runtime_secret_store_cli import main

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
