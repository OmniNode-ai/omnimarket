# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Why the Claude review key budget chose a key or none (OMN-20967)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumClaudeReviewBudgetReason(StrEnum):
    """The budget decision's reason; every value but KEY_CHOSEN means no key."""

    KEY_CHOSEN = "key_chosen"
    """The lowest-index key that keeps its reserve after the call's worst case."""

    NO_KEYS = "no_keys"
    """The request named no key."""

    NO_KEY_ABOVE_RESERVE = "no_key_above_reserve"
    """Every key would fall below its reserve if the call cost its worst case."""

    MODEL_NOT_PRICED = "model_not_priced"
    """The pricing manifest does not carry a model the decision must price."""
