# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The decision the panel makes on one incoming publication."""

from __future__ import annotations

from enum import StrEnum


class EnumPanelDecision(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    SUPERSEDED = "superseded"
    KEY_MISMATCH = "key_mismatch"
    UNDECODABLE = "undecodable"
    FUTURE_DATED = "future_dated"


__all__ = ["EnumPanelDecision"]
