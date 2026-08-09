# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for node_seam_match_compute (OMN-15763).

COMPUTE node: pure canonical seam-projection/v1 serializer + three-leg
seam-match classifier. Per operator direction 2026-08-08 ("serialize the
contracts, use them for comparison"), the match method is NOT bespoke
comparison logic — each side is reduced to its wire-crossing
``ModelSeamProjection``, and the verdict is a structural diff of the two
canonical serializations naming the exact mismatching field path.

Per ONEX rules: COMPUTE returns ``result`` (required). No I/O: live probe
results (``observed_producer`` / ``observed_consumer``) are supplied as
input by the caller (``node_seam_probe_effect``, not built in this PR),
never fetched by this node.
"""

from __future__ import annotations

from omnimarket.nodes.node_seam_match_compute.models.model_seam_match_request import (
    ModelSeamMatchRequest,
)
from omnimarket.nodes.node_seam_match_compute.models.model_seam_match_verdict import (
    EnumSeamMatchVerdict,
    EnumSeamRegenerabilityClass,
    ModelSeamLegResult,
    ModelSeamMatchVerdict,
    ModelSeamStaleProofCheck,
)
from omnimarket.seams.canonical import canonical_sha256
from omnimarket.seams.models.model_seam_projection import ModelSeamProjection

__all__ = ["HandlerSeamMatch", "check_stale_proof"]

# Fixed comparison order so "the first mismatching field" is deterministic
# regardless of pydantic field-declaration order.
_SCALAR_COMPARISON_FIELDS: tuple[str, ...] = (
    "topic",
    "envelope_model",
    "envelope_version",
    "delivery_semantics",
)


def _comparable_view(projection: ModelSeamProjection) -> dict[str, object]:
    """Wire-crossing fields only — edge_id/role/schema_version are identity,
    not comparison targets (a producer and consumer projection of the same
    edge always differ on ``role`` by construction)."""

    return {
        "topic": projection.topic,
        "envelope_model": projection.envelope_model,
        "envelope_version": projection.envelope_version,
        "delivery_semantics": projection.delivery_semantics.value,
        "key_fields": [f.model_dump(mode="json") for f in projection.key_fields],
    }


def _first_mismatching_field_path(
    expected: ModelSeamProjection, actual: ModelSeamProjection
) -> str | None:
    """Name the exact first differing field path — never a boolean."""

    exp = _comparable_view(expected)
    act = _comparable_view(actual)

    for field in _SCALAR_COMPARISON_FIELDS:
        if exp[field] != act[field]:
            return field

    exp_fields = exp["key_fields"]
    act_fields = act["key_fields"]
    assert isinstance(exp_fields, list)
    assert isinstance(act_fields, list)
    if len(exp_fields) != len(act_fields):
        return "key_fields"
    for index, (exp_field, act_field) in enumerate(
        zip(exp_fields, act_fields, strict=True)
    ):
        if exp_field["name"] != act_field["name"]:
            return f"key_fields[{index}].name"
        if exp_field["field_type"] != act_field["field_type"]:
            return f"key_fields[{index}].field_type"

    return None


def _leg(
    expected: ModelSeamProjection | None, actual: ModelSeamProjection | None
) -> ModelSeamLegResult:
    """One leg of the three-leg composition. ``passed=None`` (not evaluated)
    when either side is absent — this is what keeps a shape-only match out
    of REGENERABLE by construction rather than by a separate flag."""

    if expected is None or actual is None:
        return ModelSeamLegResult(passed=None)
    path = _first_mismatching_field_path(expected, actual)
    return ModelSeamLegResult(passed=path is None, mismatching_field_path=path)


def _stale_proof_for_request(
    request: ModelSeamMatchRequest, producer: ModelSeamProjection | None
) -> ModelSeamStaleProofCheck | None:
    """Wire the stale-proof detector into every ``handle`` return path.

    ``None`` (not a false "not stale") when there is nothing to check — no
    ``pinned_hash`` was supplied, or there is no declared producer to check
    it against.
    """

    if request.pinned_hash is None or producer is None:
        return None
    return check_stale_proof(
        edge_id=request.edge_id,
        pinned_hash=request.pinned_hash,
        current_producer=producer,
    )


class HandlerSeamMatch:
    """ONEX compute handler for the three-leg seam-match classification."""

    def handle(self, request: ModelSeamMatchRequest) -> ModelSeamMatchVerdict:
        producer = request.declared_producer
        consumer = request.declared_consumer

        declared_producer_hash = canonical_sha256(producer) if producer else None
        declared_consumer_hash = canonical_sha256(consumer) if consumer else None
        stale_proof = _stale_proof_for_request(request, producer)

        if producer is None or consumer is None:
            # UNMATCHED: a produced seam has no consumer, or a consumed
            # seam has no producer. Neither declared side is comparable, so
            # regenerability is undefined.
            not_evaluated = ModelSeamLegResult(passed=None)
            return ModelSeamMatchVerdict(
                edge_id=request.edge_id,
                verdict=EnumSeamMatchVerdict.UNMATCHED,
                regenerability=EnumSeamRegenerabilityClass.NOT_APPLICABLE,
                leg1_declared_vs_declared=not_evaluated,
                leg2_observed_producer_vs_declared=not_evaluated,
                leg3_observed_consumer_vs_declared=not_evaluated,
                declared_producer_hash=declared_producer_hash,
                declared_consumer_hash=declared_consumer_hash,
                stale_proof=stale_proof,
            )

        leg1 = _leg(producer, consumer)
        verdict = (
            EnumSeamMatchVerdict.MATCHED
            if leg1.passed
            else EnumSeamMatchVerdict.MISMATCH
        )

        if verdict is EnumSeamMatchVerdict.MISMATCH:
            # A leg-1 shape mismatch makes observed-vs-declared meaningless:
            # there is no single declared shape to observe against.
            not_evaluated = ModelSeamLegResult(passed=None)
            return ModelSeamMatchVerdict(
                edge_id=request.edge_id,
                verdict=verdict,
                regenerability=EnumSeamRegenerabilityClass.NOT_APPLICABLE,
                leg1_declared_vs_declared=leg1,
                leg2_observed_producer_vs_declared=not_evaluated,
                leg3_observed_consumer_vs_declared=not_evaluated,
                declared_producer_hash=declared_producer_hash,
                declared_consumer_hash=declared_consumer_hash,
                stale_proof=stale_proof,
            )

        leg2 = _leg(producer, request.observed_producer)
        leg3 = _leg(consumer, request.observed_consumer)

        # §0.3 regeneration-boundary rule: REGENERABLE requires all three
        # legs explicitly green. Anything less (including "not evaluated")
        # is SHAPE_ONLY — a contract.yaml-vs-contract.yaml shape comparison
        # never counts as regenerable, full stop.
        regenerability = (
            EnumSeamRegenerabilityClass.REGENERABLE
            if leg2.passed is True and leg3.passed is True
            else EnumSeamRegenerabilityClass.SHAPE_ONLY
        )

        return ModelSeamMatchVerdict(
            edge_id=request.edge_id,
            verdict=verdict,
            regenerability=regenerability,
            leg1_declared_vs_declared=leg1,
            leg2_observed_producer_vs_declared=leg2,
            leg3_observed_consumer_vs_declared=leg3,
            declared_producer_hash=declared_producer_hash,
            declared_consumer_hash=declared_consumer_hash,
            stale_proof=stale_proof,
        )


def check_stale_proof(
    *, edge_id: str, pinned_hash: str, current_producer: ModelSeamProjection
) -> ModelSeamStaleProofCheck:
    """Stale-proof detector: does the registry-pinned hash still match the
    current declared-producer projection's canonical hash?

    A pin mismatch means the seam changed underneath a golden/allowlist
    entry without a re-pin — the proof is stale, not merely absent.
    """

    current_hash = canonical_sha256(current_producer)
    stale = current_hash != pinned_hash
    detail = "seam changed, proof stale" if stale else "hash pin current"
    return ModelSeamStaleProofCheck(
        edge_id=edge_id,
        pinned_hash=pinned_hash,
        current_hash=current_hash,
        stale=stale,
        detail=detail,
    )
