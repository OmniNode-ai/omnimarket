# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a whole facts record was refused."""

from enum import StrEnum


class EnumRunningRevisionRefusalReason(StrEnum):
    """The facts contradict themselves; no reading is made from them."""

    DUPLICATE_REPOSITORY = "duplicate_repository"
    UNKNOWN_REPOSITORY = "unknown_repository"
    TIME_AFTER_OBSERVED_AT = "time_after_observed_at"
