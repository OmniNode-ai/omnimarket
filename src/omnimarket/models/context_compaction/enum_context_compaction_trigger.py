# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What started a compaction (OMN-21113, TA.11)."""

from __future__ import annotations

from enum import StrEnum


class EnumContextCompactionTrigger(StrEnum):
    MANUAL = "manual"
    AUTO = "auto"
    PRECOMPUTE = "precompute"


__all__ = ["EnumContextCompactionTrigger"]
