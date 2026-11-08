# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where a lane stands, read from its ledger rows and receipt."""

from __future__ import annotations

from enum import StrEnum


class EnumLaneState(StrEnum):
    """Where a lane stands, read from its ledger rows and receipt."""

    LIVE = "live"
    STOPPED = "stopped"
    DONE = "done"
