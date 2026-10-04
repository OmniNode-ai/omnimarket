# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a judge reply or a scored run was refused."""

from __future__ import annotations

from enum import StrEnum


class EnumAcceptanceIssueCode(StrEnum):
    """Why a judge reply or a scored run was refused."""

    UNPARSEABLE_REPLY = "unparseable_reply"
    BAD_SHAPE = "bad_shape"
    MISSING_ITEM = "missing_item"
    DUPLICATE_ITEM = "duplicate_item"
    UNKNOWN_ITEM = "unknown_item"
    BAD_FAILURE_CLASS = "bad_failure_class"
    BAD_QUALITY = "bad_quality"
    ACCEPT_QUALITY_TOO_LOW = "accept_quality_too_low"
    ACCEPT_WITH_FAILURE_CLASS = "accept_with_failure_class"
    REJECT_WITHOUT_FAILURE_CLASS = "reject_without_failure_class"
    BAD_REASON = "bad_reason"
    REASON_TOO_LONG = "reason_too_long"
    PRIMARY_VERDICT_MISSING = "primary_verdict_missing"
    NO_DOUBLE_JUDGED_ITEMS = "no_double_judged_items"
    SAMPLE_TOO_SMALL = "sample_too_small"
    KAPPA_BELOW_MINIMUM = "kappa_below_minimum"
