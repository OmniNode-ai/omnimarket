# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The closed list of reasons a delegation ladder ends with no answer (OMN-23165)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDelegationEndOfLadderReason(StrEnum):
    """Why no model in the ladder answered, in one word.

    A run that ends ``returned_to_caller`` carries exactly one of these on its
    terminal as ``terminal_failure_reason``. Each rung's own cause stays on that
    rung's attempt record; this names what decided the run.
    """

    EVERY_RUNG_FAILED = "every_rung_failed"
    """Rungs were tried and each failed for a reason that is none of the others."""

    EVERY_RUNG_RATE_LIMITED = "every_rung_rate_limited"
    """Every rung that was tried answered with a rate limit."""

    NO_RUNG_REACHABLE = "no_rung_reachable"
    """No rung could be called: none existed, or every one was unreachable."""

    ESCALATION_LIMIT_REACHED = "escalation_limit_reached"
    """The task class's escalation budget was spent before any answer was accepted."""

    QUALITY_VETO = "quality_veto"
    """The quality gate refused the answers the ladder got, and accepted none."""


__all__: list[str] = ["EnumDelegationEndOfLadderReason"]
