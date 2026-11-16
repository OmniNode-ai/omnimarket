# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one handled input did."""

from __future__ import annotations

from enum import StrEnum


class EnumMergeOverrideOutcome(StrEnum):
    RECEIPT_RECORDED = "receipt_recorded"
    READ_REQUESTED = "read_requested"
    DUPLICATE = "duplicate"
    VERDICT = "verdict"
    IGNORED = "ignored"
    REFUSED = "refused"
    SWEPT = "swept"
    THROTTLED = "throttled"
