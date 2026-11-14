# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why one tip-change event was refused instead of batched."""

from enum import StrEnum


class EnumDevRollEventRefusal(StrEnum):
    """A refused event is recorded on the decision, never dropped."""

    UNKNOWN_REPOSITORY = "unknown_repository"
    EVENT_AFTER_OBSERVED_AT = "event_after_observed_at"
