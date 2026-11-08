# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sources the work-state projection reads, each under its own declared budget."""

from __future__ import annotations

from enum import StrEnum


class EnumWorkStateSource(StrEnum):
    LEDGER = "ledger"
    PR_WATCHER = "pr-watcher"
    LINEAR = "linear"
    PLANS = "plans"
    RECEIPTS = "receipts"
    READINGS = "readings"


__all__ = ["EnumWorkStateSource"]
