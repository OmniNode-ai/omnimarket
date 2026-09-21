# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The futility verdict is derived, never emitted as a wire key (OMN-19056).

OMN-19016 (omnimarket#2755, squash ``1d957613d``, dev 0.4.175) added
``no_rung_can_satisfy`` as a pydantic FIELD on ``ModelQualityGateResult``. That
model declares ``extra="forbid"``. The last released omnimarket is **v0.4.166**
and its copy of the model has no such field, so every deployed consumer at the
release refuses the payload outright at its decode boundary:

    no_rung_can_satisfy: Extra inputs are not permitted

This is OMN-18852 one field later, and it is worse in one respect: ``published_at``
was ``None`` by default and could be excluded while unset, buying a quiet window.
This field defaults to ``False`` and ``False`` serialises, so it went on the wire
on EVERY dump from the moment it merged. There was no quiet window at all.

**Why ``exclude_if`` -- the OMN-18852 remedy -- is not the remedy here.** The
OMN-18868 wire-compatibility gate says so in its own docstring: it grades the
MAXIMAL shape a producer can emit, so a new optional field is a finding even
when the producer excludes it while unset, and it has no waiver list. It is
right to: an excluded-while-unset field is still refused the moment anything
sets it, and setting it is the entire reason the field exists.

**What is done instead.** The value was never independent information. The
producer computed it as a predicate over ``failure_reasons`` -- "is every reason
refusing this response a ``SHAPE_REFUSED`` one" -- and ``failure_reasons`` is a
plain ``tuple[str, ...]`` that v0.4.166 already declares, already accepts, and
carries the prefix through untouched. So the field is withdrawn and the verdict
is derived by a ``property`` on the model. Every consumer that could have read
the field computes it instead; every consumer too old to know either keeps the
pre-OMN-19016 climb-always behaviour, exactly as the withdrawn field's own
description promised.

**OMN-19016's behaviour is NOT reverted by any of this**, and the tests below
pin that as hard as they pin the wire shape: the ``all``-not-``any`` derivation
invariant, the zeroed score, the ``SHAPE_REFUSED`` verdict and both consumer
read sites are unchanged. Only the serialisation changed. ``tests/unit/delegation/
test_omn19016_veto_zeroes_score_and_stops_climb.py`` and ``tests/unit/nodes/
node_delegate_skill_orchestrator/test_omn19016_shape_veto_stops_the_ladder.py``
must stay green beside this module, and do.

Re-emitting the key as a real field is step 2 and is a child ticket of
OMN-19056, gated on a release that tolerates it being deployed first.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from omnimarket.models.delegation.wire.model_quality_gate import (
    ModelQualityGateResult,
)

# Spelled as LITERALS, not imported from the module under test, and the reason
# is the same one OMN-19016's own tests give: this module must still COLLECT
# against the parent commit, where the withdrawal has not happened. Importing
# the two constants would turn the red into an ImportError, which proves only
# that a symbol is missing -- never that the wire is broken. With literals the
# failure is behavioural: the released consumer refuses a real emitted payload.
NO_RUNG_CAN_SATISFY_WIRE_KEY = "no_rung_can_satisfy"
SHAPE_REFUSED_VERDICT_PREFIX = "SHAPE_REFUSED"

# The exact wire keys ``ModelQualityGateResult`` emits at the LAST RELEASED
# omnimarket, v0.4.166 -- read off ``git show v0.4.166:src/omnimarket/models/
# delegation/wire/model_quality_gate.py`` and pinned here as a literal.
#
# Pinned rather than derived, for the same reason OMN-18852's module pins its
# equivalent: a set computed from the model under test agrees with ANY change
# that model makes, including the one that caused the break. A literal is the
# only form of this constant that can be wrong in the direction that matters.
#
# ``pass`` rather than ``pass_``: the field carries ``alias="pass"`` and the
# model sets ``serialize_by_alias=True``, so ``pass`` is what goes on the wire.
# ``no_rung_can_satisfy`` is deliberately absent -- that absence IS this module.
_RELEASED_V0_4_166_WIRE_KEYS: frozenset[str] = frozenset(
    {
        "acceptance_command",
        "acceptance_version",
        "actual_score",
        "corpus_hash",
        "correlation_id",
        "fail_category",
        "failure_cases",
        "failure_reasons",
        "fallback_recommended",
        "finish_reason",
        "pass",
        "passed",
        "quality_score",
        "reasoning_preamble",
        "reasoning_preamble_rule",
        "rule_evaluations",
        "score_source",
        "skipped_checks",
        "ungrounded_identifiers",
        "validator_or_artifact_hash",
    }
)

#: The refusal the deployed v0.4.166 consumer produces, byte for byte. Spelled
#: as a literal so a pydantic change that reworded it is a red test here rather
#: than a silent weakening of the assertion.
_EXTRA_FORBIDDEN_MESSAGE = "Extra inputs are not permitted"


def _released_consumer_replica() -> type[BaseModel]:
    """A stand-in for the v0.4.166 ``ModelQualityGateResult`` decode boundary.

    Two versions of one package cannot both be imported into one interpreter,
    which is why the OMN-18868 GATE runs the released tree in a subprocess. A
    unit test does not get to spend a subprocess and a ``git archive`` per case,
    so this replica reproduces the only two things that decide the outcome: the
    released model's ``extra="forbid"`` policy and the exact set of keys it
    declares. Both are pinned literals above.

    Field types are deliberately permissive. The question asked here is a SHAPE
    question -- "does the released consumer accept the set of keys today's
    producer emits" -- and typing the replica faithfully would make it fail on
    value mismatches that say nothing about the wire, which is the same reason
    the real gate grades only ``extra_forbidden`` and ``missing``.
    """
    fields: dict[str, Any] = dict.fromkeys(
        sorted(_RELEASED_V0_4_166_WIRE_KEYS), (Any, None)
    )
    return create_model(  # type: ignore[call-overload,no-any-return]
        "ReleasedQualityGateResultV0_4_166",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


def _emitted_wire_payload(**overrides: Any) -> dict[str, Any]:
    """The wire form of a gate result, exactly as a producer publishes it."""
    payload: dict[str, Any] = {
        "correlation_id": str(uuid4()),
        "passed": False,
        "fail_category": "fail_heuristic",
        "quality_score": 0.0,
        "failure_reasons": (
            f"{SHAPE_REFUSED_VERDICT_PREFIX}: response is a bare single-word "
            "fragment, fails semantic_adequacy",
        ),
    }
    payload.update(overrides)
    result = ModelQualityGateResult(**payload)
    return dict(json.loads(result.model_dump_json(by_alias=True)))


# ---------------------------------------------------------------------------
# The refusal itself, reproduced against the released consumer
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_released_consumer_refuses_the_withdrawn_key_byte_for_byte() -> None:
    """The v0.4.166 refusal, reproduced verbatim. A control, green either way.

    This asserts a property of the RELEASED model, not of ours, so it passes
    before and after the fix. Its job is to prove the replica genuinely refuses
    the key -- without it, the next test could pass because the replica accepts
    everything, which is the quiet green this whole epic exists to end.
    """
    released = _released_consumer_replica()
    payload = {**_emitted_wire_payload(), NO_RUNG_CAN_SATISFY_WIRE_KEY: False}

    with pytest.raises(ValidationError) as caught:
        released.model_validate(payload)

    errors = [
        error
        for error in caught.value.errors()
        if error["loc"] == (NO_RUNG_CAN_SATISFY_WIRE_KEY,)
    ]
    assert errors, (
        "OMN-19056: the v0.4.166 replica did not refuse "
        f"{NO_RUNG_CAN_SATISFY_WIRE_KEY!r} at all, so every other assertion in "
        f"this module is vacuous. Errors raised: {caught.value.errors()}"
    )
    assert errors[0]["type"] == "extra_forbidden"
    assert errors[0]["msg"] == _EXTRA_FORBIDDEN_MESSAGE


@pytest.mark.unit
def test_the_released_consumer_accepts_a_payload_without_the_key() -> None:
    """The negative control for the test above.

    A replica that refused everything would make the refusal above meaningless.
    """
    released = _released_consumer_replica()

    decoded = released.model_validate(_emitted_wire_payload())

    assert decoded is not None


@pytest.mark.unit
def test_todays_producer_is_decoded_by_the_released_consumer() -> None:
    """THE REGRESSION. RED on dev at 0.4.175, green on this branch.

    Before the fix this raised ``no_rung_can_satisfy: Extra inputs are not
    permitted`` -- the live break the OMN-18868 gate caught on its first day.
    """
    released = _released_consumer_replica()

    try:
        released.model_validate(_emitted_wire_payload())
    except ValidationError as exc:  # pragma: no cover - the failure path
        pytest.fail(
            "OMN-19056: the last released consumer (v0.4.166) cannot decode "
            "what this tree emits, so every deployed consumer at the release "
            f"dead-letters it. This is OMN-18852 verbatim.\n{exc}"
        )


@pytest.mark.unit
def test_every_key_this_tree_emits_is_one_the_release_already_declares() -> None:
    """Consumer-first stated as a property rather than as a release order.

    Broader than the case above: it holds for a passing result, a mixed refusal
    and a fully-populated acceptance record alike, so a SECOND field added to
    this model without a release in front of it is caught here too.
    """
    emitted: set[str] = set()
    emitted |= set(_emitted_wire_payload())
    emitted |= set(_emitted_wire_payload(passed=True, fail_category="pass"))
    emitted |= set(
        _emitted_wire_payload(
            actual_score=0.5,
            pass_=False,
            score_source="combined",
            acceptance_version="delegation-deterministic-acceptance.v1",
        )
    )

    unknown_to_the_release = emitted - _RELEASED_V0_4_166_WIRE_KEYS
    assert not unknown_to_the_release, (
        f"OMN-19056: this tree emits {sorted(unknown_to_the_release)}, which "
        "the v0.4.166 consumer declares no field for and forbids as an extra. "
        "A new key on this contract lands consumer-first: release a consumer "
        "that accepts it before merging the producer that emits it."
    )


# ---------------------------------------------------------------------------
# The shape of the remedy -- pinned so it cannot be undone by accident
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_verdict_is_neither_a_field_nor_a_computed_field() -> None:
    """A ``computed_field`` would re-break the wire INVISIBLY to the gate.

    The OMN-18868 gate reads ``model_fields`` and never
    ``model_computed_fields``, so restoring the verdict as a computed field
    would put the key back on the wire and report green while doing it. That
    is strictly worse than the original defect, so the shape is pinned here
    rather than left to a comment someone has to read.
    """
    assert NO_RUNG_CAN_SATISFY_WIRE_KEY not in ModelQualityGateResult.model_fields
    assert (
        NO_RUNG_CAN_SATISFY_WIRE_KEY not in ModelQualityGateResult.model_computed_fields
    )
    assert isinstance(
        getattr(type(ModelQualityGateResult), NO_RUNG_CAN_SATISFY_WIRE_KEY, None)
        or ModelQualityGateResult.__dict__[NO_RUNG_CAN_SATISFY_WIRE_KEY],
        property,
    )


@pytest.mark.unit
def test_the_withdrawn_key_is_absent_from_every_dump() -> None:
    """Both dump forms, because a caller may use either."""
    result = ModelQualityGateResult(
        correlation_id=uuid4(),
        passed=False,
        quality_score=0.0,
        failure_reasons=(f"{SHAPE_REFUSED_VERDICT_PREFIX}: fragment",),
    )

    assert NO_RUNG_CAN_SATISFY_WIRE_KEY not in result.model_dump()
    assert NO_RUNG_CAN_SATISFY_WIRE_KEY not in json.loads(result.model_dump_json())


@pytest.mark.unit
def test_a_payload_still_carrying_the_key_is_tolerated_not_refused() -> None:
    """The forward half of consumer-first, and what unblocks step 2.

    A producer on dev 0.4.175 exactly -- never released, never deployed, but
    reachable by anyone sitting on that commit -- still emits the key. This
    model accepts and drops it rather than refusing, which also makes the NEXT
    release the tolerant consumer that lets step 2 re-add the field and pass
    the gate.
    """
    payload = {
        **_emitted_wire_payload(),
        NO_RUNG_CAN_SATISFY_WIRE_KEY: True,
    }

    decoded = ModelQualityGateResult.model_validate(payload)

    assert decoded.no_rung_can_satisfy is True, (
        "the derived verdict must agree with what the 0.4.175 producer sent, "
        "because both compute one predicate over the same failure_reasons"
    )
    assert NO_RUNG_CAN_SATISFY_WIRE_KEY not in decoded.model_dump(), (
        "a tolerated key must not be re-emitted, or the consumer becomes a "
        "producer of the break it just absorbed"
    )


# ---------------------------------------------------------------------------
# OMN-19016's derivation invariant, unchanged by the carriage change
# ---------------------------------------------------------------------------


def _result_with(*reasons: str) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=uuid4(),
        passed=not reasons,
        quality_score=0.0 if reasons else 1.0,
        failure_reasons=reasons,
    )


@pytest.mark.unit
def test_a_whole_shape_refusal_is_unsatisfiable() -> None:
    """OMN-19016's positive case survives the move from field to property."""
    assert _result_with(f"{SHAPE_REFUSED_VERDICT_PREFIX}: fragment").no_rung_can_satisfy


@pytest.mark.unit
def test_one_climbable_reason_beside_a_shape_refusal_still_climbs() -> None:
    """``all``, not ``any`` -- OMN-19016's asymmetry, verbatim.

    A costlier rung still has something to cure, so the ladder keeps its
    escalation. This is the case that makes the verdict safe to derive.
    """
    assert not _result_with(
        f"{SHAPE_REFUSED_VERDICT_PREFIX}: fragment",
        "WEAK_OUTPUT: response is truncated",
    ).no_rung_can_satisfy


@pytest.mark.unit
def test_a_climbable_refusal_alone_still_climbs() -> None:
    assert not _result_with("WEAK_OUTPUT: response is truncated").no_rung_can_satisfy


@pytest.mark.unit
def test_a_passing_result_is_never_reported_unsatisfiable() -> None:
    """The falsifier for "zero everything": an empty veto is not a veto."""
    assert not _result_with().no_rung_can_satisfy
