# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which subsystem decided a delegation run, read from its own attempt record.

OMN-19004. A terminal's ``terminal_failure_cause`` names the event that DECIDED
the run, never the last thing that went wrong on the ladder. Those coincide
often enough that the field looks right until it matters:

* ``73aba966``: five rungs, every one answered, every one refused by the
  quality gate, and the terminal read ``provider_error``.
* ``6ce51f77``: three rungs answered and were refused by the gate, the fourth
  hit a real HTTP 429, and the whole run read ``provider_quota_exhausted``.

The attempt record already says which subsystem refused each rung, through the
typed accept/climb pair recorded on every rung (OMN-16932). This module is the
one place that reading lives, so the workflow terminal and the consumer-facing
delegate-skill terminal cannot disagree about it.

The rule is deliberately simple. **A ladder on which the quality gate refused
at least one answer, and accepted none, was decided by the gate.** A provider
fault on some other rung is real and stays legible on that rung's own record,
but it only stopped the ladder collecting more answers; every answer the ladder
did collect was refused by the gate. A ladder on which no rung was answered and
judged at all was decided by the provider, and keeps its provider cause.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)

GATE_REFUSAL_REASONS: frozenset[EnumDelegationAcceptanceReason] = frozenset(
    {
        EnumDelegationAcceptanceReason.DETERMINISTIC_FLOOR_FAILED,
        EnumDelegationAcceptanceReason.ACCEPTANCE_CRITERIA_FAILED,
        EnumDelegationAcceptanceReason.SCORE_BELOW_REQUIRED_BAR,
        EnumDelegationAcceptanceReason.HEURISTIC_VETO,
    }
)
"""The accept/climb reasons that mean the gate judged an answer and refused it.

Excluded on purpose:

* ``PROVIDER_CALL_FAILED``: the rung's call failed, so there was no answer to
  judge. That is a provider event.
* ``REQUIRED_BAR_UNRESOLVED``: no bar resolved, so no judgement was reachable.
  That is a configuration fault, not a gate verdict.
* ``QUALITY_BAR_MET`` and ``JUDGE_UNAVAILABLE_DETERMINISTIC_FLOOR``: acceptances.
"""

_REFUSING_DECISIONS: frozenset[EnumDelegationAcceptanceDecision] = frozenset(
    {
        EnumDelegationAcceptanceDecision.CLIMB,
        EnumDelegationAcceptanceDecision.TERMINATE,
    }
)


def is_gate_refusal(
    decision: EnumDelegationAcceptanceDecision | None,
    reason: EnumDelegationAcceptanceReason | None,
) -> bool:
    """Whether one rung was answered and then refused by the quality gate.

    Both halves of the typed pair are required. A rung with no recorded
    decision never reached the gate (a transport skip on the bus-less path, or
    a record that predates OMN-16932), and that absence is not read as a
    refusal.
    """
    return decision in _REFUSING_DECISIONS and reason in GATE_REFUSAL_REASONS


def ladder_is_gate_decided(
    rungs: Sequence[
        tuple[
            EnumDelegationAcceptanceDecision | None,
            EnumDelegationAcceptanceReason | None,
        ]
    ],
) -> bool:
    """Whether the gate decided a ladder, given each rung's typed accept/climb pair.

    True when at least one rung was answered and refused by the gate and no
    rung was accepted. An accepted rung ends the escalation in acceptance, so a
    ladder that carries one was not decided by a refusal of any kind.
    """
    if any(
        decision is EnumDelegationAcceptanceDecision.ACCEPT for decision, _ in rungs
    ):
        return False
    return any(is_gate_refusal(decision, reason) for decision, reason in rungs)


__all__: list[str] = [
    "GATE_REFUSAL_REASONS",
    "is_gate_refusal",
    "ladder_is_gate_decided",
]
