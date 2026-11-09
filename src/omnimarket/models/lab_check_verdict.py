# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-check verdict shared by the landing decision and the lab check run effect."""

from __future__ import annotations

from enum import StrEnum


class EnumLabCheckVerdict(StrEnum):
    """What the decision reads off a head's lab-bound check runs."""

    PASS = "pass"
    FAILED = "failed"
    PENDING = "pending"
    MISSING = "missing"
    SUPERSEDED = "superseded"


__all__ = ["EnumLabCheckVerdict"]
