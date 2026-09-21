# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19004 — a terminal may not contradict its own attempt records.

The standing invariant on this ticket is that a summary field is DERIVED from
the record rather than SET alongside it, and that a terminal violating that is
refused at construction with the contradiction named. This module lands the one
clause of it that needs no vocabulary the wire cannot yet carry.

``attempts_count`` and ``attempts`` are the pair. The producers already work
hard to keep the count truthful — ``_truthful_attempts_count`` exists because
OMN-15464 measured a terminal reporting ``attempts_count=2`` while carrying an
escalation history of FOUR rejected attempts in the same payload, on
correlation ``8371bb34-3aa4-48d6-bdce-dffae3eb4b7f``. What was never closed is
that the MODEL accepts that shape. A derivation that lives only in the producer
protects only the producers that call it; the contradiction stays constructible
for the bus-less dispatch port, a direct construction, or a future adapter,
which is the same relocation the OMN-15464 label fix had to be repeated for.

THE ASYMMETRY IS THE WHOLE DESIGN, so it is stated before the tests rather than
discovered in them. A count BELOW the recorded list is a contradiction with no
reading: nothing can record more attempts than it made. A count ABOVE the list
is legitimate and common — the escalation-history fallback carries rejected
attempts only, and ``_authoritative_attempt_ladder_verdict`` reads exactly that
inequality to decide the ladder is incomplete and withhold a verdict. So this
refuses one direction and must leave the other alone, and there is a control
for each.

DELIBERATELY NOT HERE, and the reason is the wire, not the argument.

* The gate-decided ``terminal_failure_cause``. The vocabulary has no member for
  a run the quality gate decided, so the truthful value does not exist to
  assign yet. That member is OMN-19060 in the core library and has to travel
  through a release and a pin bump before anything may emit it. Refusing the
  contradiction here before the replacement exists would make the measured
  fixtures unconstructible with nothing to put in their place, so the two
  fixtures have a control below asserting they still construct.
* A zero ``attempts_count`` for a run that made no attempt at all. Twenty-five
  recorded receipts on this host report ``attempts_count=1`` beside an empty
  attempt list, because the field is floored at one and cannot say "none".
  Fixing that means widening the accepted range on a wire field, which a
  released consumer pinned to the old range would refuse — the same
  consumer-first hazard as the enum, and it lands the same way, consumer
  first.

This clause is safe to land alone because it only NARROWS what constructs. A
released consumer already accepts every payload that will still be emitted.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)

pytestmark = pytest.mark.unit


def _attempt(tier: str = "local", *, passed: bool = False) -> dict[str, Any]:
    return {
        "tier": tier,
        "backend_id": "b",
        "model_id": "m",
        "quality_gate_passed": passed,
        "quality_score": 0.0,
        "cost_usd": 0.0,
    }


def _terminal(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "correlation_id": str(uuid4()),
        "status": "failed",
        "task_type": "document",
        "quality_gate_passed": False,
        "quality_score": 0.0,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# The contradiction, in the shape it was measured in.
# ---------------------------------------------------------------------------


def test_a_terminal_cannot_record_more_attempts_than_it_counted() -> None:
    """RED: the OMN-15464 shape, two counted beside four recorded.

    Measured on correlation ``8371bb34-3aa4-48d6-bdce-dffae3eb4b7f``. The
    producer-side derivation that closed it is real and is not being replaced;
    what this adds is that the model refuses the shape, so a producer that does
    not call that derivation cannot publish it either.
    """
    payload = _terminal(
        attempts_count=2,
        attempts=[
            _attempt("local"),
            _attempt("local"),
            _attempt("local"),
            _attempt("cheap_cloud"),
        ],
    )

    with pytest.raises(ValidationError) as excinfo:
        ModelDelegateSkillResponse.model_validate(payload)

    message = str(excinfo.value)
    assert "attempts_count" in message
    # The contradiction is NAMED, with both numbers, so a reader does not have
    # to reconstruct which two fields disagreed or by how much.
    assert "2" in message, message
    assert "4" in message, message


def test_the_refusal_names_both_numbers_at_the_boundary() -> None:
    """One short of the record is still a contradiction, and still reported."""
    payload = _terminal(
        attempts_count=1,
        attempts=[_attempt("local"), _attempt("local")],
    )

    with pytest.raises(ValidationError) as excinfo:
        ModelDelegateSkillResponse.model_validate(payload)

    assert "attempts_count" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Controls. The refusal must be exactly one-directional.
# ---------------------------------------------------------------------------


def test_a_count_matching_the_record_constructs() -> None:
    """Positive control: the ordinary complete ladder is untouched."""
    payload = _terminal(
        attempts_count=3,
        attempts=[_attempt("local"), _attempt("local"), _attempt("cheap_cloud")],
    )

    model = ModelDelegateSkillResponse.model_validate(payload)

    assert model.attempts_count == 3
    assert len(model.attempts) == 3


def test_a_count_above_the_record_still_constructs() -> None:
    """Positive control, and the one that matters most.

    The escalation-history fallback carries rejected attempts only, so a count
    above the list is the normal shape of an incomplete ladder, and
    ``_authoritative_attempt_ladder_verdict`` reads that very inequality to
    withhold a success verdict. Refusing this direction would break the
    incomplete-ladder reading and every terminal that uses it.
    """
    payload = _terminal(attempts_count=5, attempts=[_attempt("local")])

    model = ModelDelegateSkillResponse.model_validate(payload)

    assert model.attempts_count == 5
    assert len(model.attempts) == 1


def test_a_terminal_with_no_recorded_attempts_still_constructs() -> None:
    """Positive control: the 25 measured receipts with an empty attempt list.

    ``attempts_count=1`` beside an empty list is the field being unable to say
    "none", which is the separate consumer-first widening named in the module
    docstring. It is a count ABOVE the record, so this clause leaves it alone,
    and it must keep constructing until that widening lands.
    """
    model = ModelDelegateSkillResponse.model_validate(
        _terminal(status="timeout", attempts_count=1, attempts=[])
    )

    assert model.attempts_count == 1
    assert model.attempts == []


# ---------------------------------------------------------------------------
# Derived, not defaulted. This is the clause the ticket is named for.
# ---------------------------------------------------------------------------


def test_an_absent_count_is_derived_from_the_attempt_list() -> None:
    """RED: the field defaulted to 1 while the record carried three rungs.

    Not hypothetical. The bus-less dispatch port builds its terminal payload
    with the attempt list and no count, so a three-rung refusal published
    ``attempts_count=1`` beside three records. The default was asserting one
    attempt regardless of how many had just been recorded, which is a summary
    field SET beside the record rather than DERIVED from it.
    """
    payload = _terminal(
        attempts=[_attempt("local"), _attempt("local"), _attempt("cheap_cloud")]
    )
    assert "attempts_count" not in payload

    model = ModelDelegateSkillResponse.model_validate(payload)

    assert model.attempts_count == 3


def test_an_explicit_count_stays_authoritative() -> None:
    """Derivation fills a gap; it never overrules a producer that stated one.

    A count above the list is the legitimate incomplete-ladder shape, and the
    producer is entitled to state it. Deriving over the top of that would
    destroy the very reading the other control protects.
    """
    payload = _terminal(attempts_count=9, attempts=[_attempt("local")])

    model = ModelDelegateSkillResponse.model_validate(payload)

    assert model.attempts_count == 9


def test_an_empty_attempt_list_keeps_the_floor_of_one() -> None:
    """The zero case is a wire widening, so derivation must not reach for it.

    A run that made no attempt at all should say zero. The field is bounded at
    one, and lowering that bound is a change a released consumer pinned to the
    old range would refuse, so it lands consumer-first and not here. Deriving
    zero would produce a value this model cannot even hold.
    """
    model = ModelDelegateSkillResponse.model_validate(
        _terminal(status="timeout", attempts=[])
    )

    assert model.attempts_count == 1


# ---------------------------------------------------------------------------
# The measured fixtures stay constructible until the vocabulary lands.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("correlation_id", "cause", "counted", "recorded"),
    [
        pytest.param(
            "73aba966-970c-4f29-987e-d85246152b2d",
            "provider_error",
            5,
            5,
            id="gate_refused_every_rung_reported_as_provider_error",
        ),
        pytest.param(
            "6ce51f77-62c4-4785-93f5-42e06e6a0a67",
            "provider_quota_exhausted",
            4,
            4,
            id="gate_decided_but_reported_as_quota_exhausted",
        ),
    ],
)
def test_the_untruthful_cause_fixtures_still_construct(
    correlation_id: str, cause: str, counted: int, recorded: int
) -> None:
    """The two runs this ticket exists for must NOT become unconstructible yet.

    Both terminals name a provider cause for a run the quality gate decided.
    That is the defect, and it is not repaired here, because the vocabulary has
    no member that could replace the value. Refusing them now would take a
    wrong-but-present cause and leave nothing, which is not an improvement.

    This control exists so that the follow-up which DOES refuse them has to
    delete a test that says why they were allowed, rather than silently
    tightening a clause and discovering the fixtures at replay time.
    """
    payload = _terminal(
        correlation_id=correlation_id,
        terminal_failure_cause=cause,
        attempts_count=counted,
        attempts=[_attempt("local") for _ in range(recorded)],
    )

    model = ModelDelegateSkillResponse.model_validate(payload)

    assert model.terminal_failure_cause is not None
    assert model.terminal_failure_cause.value == cause
    assert model.attempts_count == counted
