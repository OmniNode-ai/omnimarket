# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a committed run was not replayed."""

from __future__ import annotations

from enum import StrEnum


class EnumAcceptanceReplayRefusal(StrEnum):
    """The closed set of reasons replay builds no events."""

    NO_TIER_FOR_MODEL = "no_tier_for_model"
    NO_CALL_TIME = "no_call_time"
    NO_PRIMARY_VERDICT = "no_primary_verdict"
    DUPLICATE_CORRELATION_ID = "duplicate_correlation_id"
    NO_CALIBRATION_FOR_PRIMARY = "no_calibration_for_primary"
    COUNT_DISAGREES_WITH_SUMMARY = "count_disagrees_with_summary"
