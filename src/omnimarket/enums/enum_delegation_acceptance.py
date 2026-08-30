# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed accept/climb decision for a delegation quality-gate verdict (OMN-16932)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDelegationAcceptanceDecision(StrEnum):
    """Whether the ladder stopped on the rung that answered, or climbed past it.

    The orchestrator has always made this decision (``quality_accepted`` in
    ``handle_gate_result``); it has never recorded it. A reader of the event
    log could only infer "it climbed" from the presence of a later provider
    call, which is exactly why an escalation past a working free rung stayed
    invisible until it showed up as a 429 on a metered bill.
    """

    ACCEPT = "accept"
    """The answering rung's response was accepted; the chain ends here."""

    CLIMB = "climb"
    """The response was rejected; the ladder moves to the next rung."""


@unique
class EnumDelegationAcceptanceReason(StrEnum):
    """Why the accept/climb decision went the way it did.

    One value per branch of the ``quality_accepted`` expression, so the reason
    is derived from the decision rather than restated beside it. The three
    CLIMB values mirror the three-way label OMN-15464 introduced for the
    human-readable reason string; recording them as an enum means a consumer
    (projection, dashboard, cost audit) never has to parse prose to learn why
    a free rung was abandoned.
    """

    QUALITY_BAR_MET = "quality_bar_met"
    """The gate passed the response and its score was at or above the bar."""

    JUDGE_UNAVAILABLE_DETERMINISTIC_FLOOR = "judge_unavailable_deterministic_floor"
    """The gate passed on the deterministic floor with no judge band (OMN-13959)."""

    DETERMINISTIC_FLOOR_FAILED = "deterministic_floor_failed"
    """A deterministic DoD check failed — a hard floor no score may lift."""

    ACCEPTANCE_CRITERIA_FAILED = "acceptance_criteria_failed"
    """The score cleared the bar but the gate rejected on an acceptance criterion."""

    SCORE_BELOW_REQUIRED_BAR = "score_below_required_bar"
    """The graded score was below the task class's required bar."""

    PROVIDER_CALL_FAILED = "provider_call_failed"
    """The rung's inference call itself failed, so there was no response to judge."""

    REQUIRED_BAR_UNRESOLVED = "required_bar_unresolved"
    """No required-bar authority resolved, so no accept/climb decision was reachable."""


__all__: list[str] = [
    "EnumDelegationAcceptanceDecision",
    "EnumDelegationAcceptanceReason",
]
