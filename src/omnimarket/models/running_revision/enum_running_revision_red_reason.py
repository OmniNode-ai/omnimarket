# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a running-revision reading is red."""

from enum import StrEnum


class EnumRunningRevisionRedReason(StrEnum):
    """One reason per red flag, in the order the handler checks them."""

    BEHIND_PAST_THRESHOLD = "behind_past_threshold"
    UNKNOWN_REPOSITORY_READING = "unknown_repository_reading"
    MEDIAN_OVER_BOUND = "median_over_bound"
