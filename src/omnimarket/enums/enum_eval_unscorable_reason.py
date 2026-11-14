# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a try could not be scored, and whose fault that was (OMN-24170)."""

from __future__ import annotations

from enum import StrEnum


class EnumEvalUnscorableClass(StrEnum):
    """Whose fault an unscorable try is.

    model           -- the model's own output could not be scored; it counts in n
                       as not passed.
    infrastructure  -- the platform failed; the try is retried and, still
                       unscorable, stays outside n.
    """

    MODEL = "model"
    INFRASTRUCTURE = "infrastructure"


class EnumEvalUnscorableReason(StrEnum):
    """The typed reason a try carries when its result is unscorable."""

    EMPTY_ANSWER = "empty_answer"
    MALFORMED_ANSWER = "malformed_answer"
    OVER_TOKEN_BUDGET = "over_token_budget"
    REVISION_UNOBSERVED = "revision_unobserved"
    REGISTRATION_MISMATCH = "registration_mismatch"
    ENDPOINT_UNAVAILABLE = "endpoint_unavailable"
    GENERATION_TIMEOUT = "generation_timeout"
    CHECK_COULD_NOT_RUN = "check_could_not_run"

    @property
    def reason_class(self) -> EnumEvalUnscorableClass:
        """The class of this reason, model or infrastructure."""
        if self in _MODEL_REASONS:
            return EnumEvalUnscorableClass.MODEL
        return EnumEvalUnscorableClass.INFRASTRUCTURE


_MODEL_REASONS = frozenset(
    {
        EnumEvalUnscorableReason.EMPTY_ANSWER,
        EnumEvalUnscorableReason.MALFORMED_ANSWER,
        EnumEvalUnscorableReason.OVER_TOKEN_BUDGET,
    }
)

__all__ = ["EnumEvalUnscorableClass", "EnumEvalUnscorableReason"]
