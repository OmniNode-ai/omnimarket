# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the host reconciliation node through its runtime adapter."""

from .runtime_worktree_reconcile import main

if __name__ == "__main__":
    raise SystemExit(main())
