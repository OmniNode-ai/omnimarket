# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How a READY PR was handed to GitHub to merge."""

from __future__ import annotations

from enum import StrEnum


class EnumPrLandingArmMethod(StrEnum):
    """Auto-merge armed, or enqueued on a merge queue, by the repo's live policy."""

    AUTO_MERGE = "auto_merge"
    QUEUE = "queue"


__all__: list[str] = ["EnumPrLandingArmMethod"]
