# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a facts record was refused instead of verdicted."""

from enum import StrEnum


class EnumDevRollVerifyRefusal(StrEnum):
    """The facts are incomplete or contradict themselves; no verdict is made."""

    ROLL_NOT_FINISHED = "roll_not_finished"
    START_AFTER_OBSERVED_AT = "start_after_observed_at"
    START_FOR_OTHER_ROLL = "start_for_other_roll"
    DUPLICATE_START_EVENT = "duplicate_start_event"
