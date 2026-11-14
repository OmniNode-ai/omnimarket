# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why the gate took an input and did not publish a decision request or a release."""

from enum import StrEnum


class EnumDevRollGateObservedReason(StrEnum):
    """One typed answer per input that yields neither facts nor a release."""

    ALREADY_HELD = "already_held"
    NOTHING_HELD = "nothing_held"
    NOT_OURS = "not_ours"
    HELD_BY_DECISION = "held_by_decision"
    ROLL_IN_PROGRESS = "roll_in_progress"
    ROLL_FINISHED = "roll_finished"
    ROLL_PENDING = "roll_pending"
    NOT_LIVE = "not_live"
    NODE_UNAVAILABLE = "node_unavailable"
