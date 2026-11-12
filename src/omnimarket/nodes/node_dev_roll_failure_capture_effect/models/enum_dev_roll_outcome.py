# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The roll's typed outcome (OMN-24013, five whys R2)."""

from __future__ import annotations

from enum import Enum


class EnumDevRollOutcome(str, Enum):
    """How the dev roll ended, as the terminal record reports it."""

    RESTORED_VERIFIED = "restored_verified"
    RESTORED_UNVERIFIED = "restored_unverified"
    RECOVERY_FAILED = "recovery_failed"
