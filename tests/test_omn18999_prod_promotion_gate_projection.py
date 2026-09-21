# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Acceptance tests for the durable prod-promotion-gate decision (OMN-18999).

Surface 2 of 4 under OMN-18946. These are the falsifiers named on the
ticket's acceptance criteria and in the node contract's ``dod_evidence``
block, and every one of them EXECUTES the gate rather than inspecting it: the
decision each test projects is the decision the real gate returned for a real
refusing input, not a hand-built payload asserting what the author hoped the
gate would say.

That distinction is the point of the ticket. A test that hand-wrote a
``reason`` string and asserted the writer stored it would have been green on
2026-09-20, while the lab lane sat frozen for two and a quarter hours behind
six correct refusals that reached no surface at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.events.runtime_deployment import (
    EnumOccGateState,
    EnumProdGateOutcome,
    EnumPromotionClass,
    EnumRuntimeLane,
    ModelProdPromotionGateCommand,
    ModelProdPromotionGrant,
    ModelReadinessProjectionFact,
)
from omnimarket.nodes.node_prod_promotion_gate_compute.handlers.handler_prod_promotion_gate import (
    evaluate_gate,
)
from omnimarket.nodes.node_projection_prod_promotion_gate.handlers.handler_prod_promotion_gate_writer import (
    ProdPromotionGateProjectionWriter,
)
from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from omnimarket.projection.morning_page import (
    TOPIC_PROMOTION_GATE,
    EnumPanelState,
    read_projection,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.snapshot_cache import SnapshotCache
from omnimarket.projection.snapshot_publisher import encode_snapshot_delta

pytestmark = pytest.mark.unit

CORRELATION = UUID("6a2f1d3e-9b47-4c58-8e21-0d5f7a3b9c14")
EVALUATED_AT = datetime(2026, 9, 20, 14, 30, tzinfo=UTC)
STABILITY_DIGEST = (
    "sha256:1111111111111111111111111111111111111111111111111111111111111111"
)
OTHER_DIGEST = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
BATCH = "promo-batch-2026-09-20"
GRANT_ID = "grant-omn-18999-01"
ROLLBACK = "sha256:3333333333333333333333333333333333333333333333333333333333333333"


# ---------------------------------------------------------------------------
# Fixtures that drive the REAL gate
# ---------------------------------------------------------------------------


def _grant(**overrides: Any) -> ModelProdPromotionGrant:
    """A grant that authorizes the happy path, with named fields replaced."""
    fields: dict[str, Any] = {
        "grant_id": GRANT_ID,
        "approved_lane": EnumRuntimeLane.PROD,
        "approved_image_digest": STABILITY_DIGEST,
        "approved_promotion_batch_id": BATCH,
        "approved_by": "jonah",
        "created_at": EVALUATED_AT - timedelta(hours=1),
        "expires_at": EVALUATED_AT + timedelta(hours=1),
    }
    fields.update(overrides)
    return ModelProdPromotionGrant(**fields)


def _readiness(**overrides: Any) -> ModelReadinessProjectionFact:
    """A stability readiness fact that satisfies the gate, with replacements."""
    fields: dict[str, Any] = {
        "readiness_state": "READY",
        "image_digest": STABILITY_DIGEST,
        "promotion_batch_id": BATCH,
    }
    fields.update(overrides)
    return ModelReadinessProjectionFact(**fields)


def _command(**overrides: Any) -> ModelProdPromotionGateCommand:
    """A prod gate command that would be ALLOWED, with named fields replaced.

    Every refusal case below is this command with ONE field changed, so the
    branch a case reaches is the branch that field controls and nothing else.
    """
    fields: dict[str, Any] = {
        "correlation_id": CORRELATION,
        "runtime_lane": EnumRuntimeLane.PROD,
        "requested_image_digest": STABILITY_DIGEST,
        "promotion_batch_id": BATCH,
        "readiness_projection": _readiness(),
        "occ_gate_state": EnumOccGateState.MERGED,
        "rollback_target": ROLLBACK,
        "requested_by": "jonah",
        "promotion_grant": _grant(),
        "evaluated_at": EVALUATED_AT,
    }
    fields.update(overrides)
    return ModelProdPromotionGateCommand(**fields)


def _decision_payload(command: ModelProdPromotionGateCommand) -> dict[str, Any]:
    """Run the real gate and serialise its decision as the bus carries it."""
    return evaluate_gate(command).model_dump(mode="json")


class _RecordingDb:
    """A database double that records every statement and its bound values.

    It walks the write path; it does not type-check the SQL. A string bound
    where a TIMESTAMPTZ or a UUID is declared is the question a double cannot
    answer, and it is the question the migration's own column types and the
    repository's real-Postgres write-path gates answer.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return [
            {
                "correlation_id": args[0],
                "outcome": args[1],
                "allowed": args[2],
                "grant_id": args[4],
                "requested_image_digest": args[5],
                "evaluated_at": args[10],
            }
        ]


def _project(
    payload: dict[str, Any], *, offset: int = 0
) -> tuple[_RecordingDb, dict[str, Any]]:
    """Drive one decision through the writer and return (db, handler result)."""
    writer = ProdPromotionGateProjectionWriter()
    db = _RecordingDb()
    writer._db = db  # type: ignore[assignment]
    data = dict(payload)
    data["_topic"] = writer.subscribe_topics[0]
    data["_partition"] = 0
    data["_offset"] = offset
    result = writer.handle(data)
    return db, result


def _bound(db: _RecordingDb) -> dict[str, Any]:
    """The single row the writer bound, as a column->value mapping."""
    assert len(db.calls) == 1, f"expected exactly one statement, got {len(db.calls)}"
    _, args = db.calls[0]
    columns = (
        "correlation_id",
        "outcome",
        "allowed",
        "reason",
        "grant_id",
        "requested_image_digest",
        "resolved_image_digest",
        "rollback_target",
        "runtime_lane",
        "promotion_batch_id",
        "evaluated_at",
        "source_topic",
        "projected_at",
    )
    assert len(args) == len(columns)
    return dict(zip(columns, args, strict=True))


# ---------------------------------------------------------------------------
# AC1 — a blocked promotion produces a queryable row with the four fields
# ---------------------------------------------------------------------------


def test_ac1_a_blocked_promotion_writes_exactly_one_row_with_the_four_fields() -> None:
    """AC1: the typed reason, grant id, requested digest and evaluation time.

    The refusal driven here is a real one: the grant authorizes a digest that
    is not the digest being promoted, which is the shape a stale grant takes.
    Before this node existed the query behind this assertion returned zero
    rows, because no relation and no subscriber existed to produce one.
    """
    command = _command(promotion_grant=_grant(approved_image_digest=OTHER_DIGEST))
    decision = evaluate_gate(command)
    assert decision.allowed is False, "fixture must drive a REFUSAL, not an allow"

    db, result = _project(decision.model_dump(mode="json"))

    assert result["rows_upserted"] == 1
    row = _bound(db)

    # The four fields the acceptance criterion names, asserted as the tree the
    # writer bound rather than as a substring of a rendered sentence.
    assert row["outcome"] == EnumProdGateOutcome.GRANT_DIGEST_MISMATCH.value
    assert row["grant_id"] == GRANT_ID
    assert row["requested_image_digest"] == STABILITY_DIGEST
    assert row["evaluated_at"] == EVALUATED_AT

    # And the refusal is stored AS a refusal, so a reader never has to infer
    # it from the presence of a reason.
    assert row["allowed"] is False
    assert row["correlation_id"] == CORRELATION
    assert row["resolved_image_digest"] is None


def test_ac1_a_redelivery_converges_on_one_row_rather_than_two() -> None:
    """The key does what the contract's dedupe_key claims it does.

    Asserted on the statement, not on a live database: the writer must emit an
    ON CONFLICT that names the declared key. A writer whose upsert conflicted
    on nothing would duplicate every redelivery while every test above stayed
    green.
    """
    command = _command(promotion_grant=None)
    db, _ = _project(_decision_payload(command))
    statement, _ = db.calls[0]
    assert "ON CONFLICT (correlation_id) DO UPDATE" in statement


# ---------------------------------------------------------------------------
# AC2 — every refusal branch is reachable in the row, not just the first
# ---------------------------------------------------------------------------

#: Every refusal the gate can reach through the COMPUTE node, as
#: (case name, command override, expected typed outcome).
#:
#: This is TWELVE branches, not the seven the ticket names. The seven is the
#: member count of ``EnumProdGrantReason``, which enumerates the authorization
#: failures only; the gate also refuses on six readiness / digest / evidence
#: facts that no grant reason describes, and one grant reason
#: (``self_granted``) has not been produced since OMN-14814 removed
#: dual-control. Parametrising over the seven would have covered six real
#: branches and asserted one that cannot fire. See
#: ``test_the_self_granted_reason_is_retained_for_the_wire_and_not_produced``.
_REFUSAL_CASES: list[tuple[str, dict[str, Any], EnumProdGateOutcome]] = [
    (
        "candidate_lineage",
        {
            "promotion_class": EnumPromotionClass.STABILITY_CANDIDATE,
            "promotion_grant": None,
        },
        EnumProdGateOutcome.CANDIDATE_NOT_AUTHORIZED,
    ),
    (
        "non_main_lineage",
        {"non_main_lineage": True, "promotion_grant": None},
        EnumProdGateOutcome.CANDIDATE_NOT_AUTHORIZED,
    ),
    (
        "no_readiness_projection",
        {"readiness_projection": None},
        EnumProdGateOutcome.STABILITY_READINESS_ABSENT,
    ),
    (
        "readiness_batch_mismatch",
        {"readiness_projection": _readiness(promotion_batch_id="some-other-batch")},
        EnumProdGateOutcome.READINESS_BATCH_MISMATCH,
    ),
    (
        "readiness_not_ready",
        {"readiness_projection": _readiness(readiness_state="BLOCKED")},
        EnumProdGateOutcome.READINESS_NOT_READY,
    ),
    (
        "digest_mismatch",
        {
            "requested_image_digest": OTHER_DIGEST,
            "promotion_grant": _grant(approved_image_digest=OTHER_DIGEST),
        },
        EnumProdGateOutcome.DIGEST_MISMATCH,
    ),
    (
        "occ_evidence_pending",
        {"occ_gate_state": EnumOccGateState.PENDING},
        EnumProdGateOutcome.OCC_EVIDENCE_NOT_DURABLE,
    ),
    (
        "occ_evidence_blocked",
        {"occ_gate_state": EnumOccGateState.BLOCKED},
        EnumProdGateOutcome.OCC_EVIDENCE_NOT_DURABLE,
    ),
    (
        "missing_rollback_target",
        {"rollback_target": None, "previous_image": None},
        EnumProdGateOutcome.MISSING_ROLLBACK_TARGET,
    ),
    (
        "missing_grant",
        {"promotion_grant": None},
        EnumProdGateOutcome.MISSING_PROMOTION_GRANT,
    ),
    (
        "grant_lane_mismatch",
        {"promotion_grant": _grant(approved_lane=EnumRuntimeLane.STABILITY_TEST)},
        EnumProdGateOutcome.GRANT_LANE_MISMATCH,
    ),
    (
        "grant_digest_mismatch",
        {"promotion_grant": _grant(approved_image_digest=OTHER_DIGEST)},
        EnumProdGateOutcome.GRANT_DIGEST_MISMATCH,
    ),
    (
        "grant_batch_mismatch",
        {"promotion_grant": _grant(approved_promotion_batch_id="some-other-batch")},
        EnumProdGateOutcome.GRANT_BATCH_MISMATCH,
    ),
    (
        "expired_grant",
        {"promotion_grant": _grant(expires_at=EVALUATED_AT - timedelta(minutes=1))},
        EnumProdGateOutcome.EXPIRED_PROMOTION_GRANT,
    ),
]


@pytest.mark.parametrize(
    ("case", "override", "expected"),
    _REFUSAL_CASES,
    ids=[name for name, _, _ in _REFUSAL_CASES],
)
def test_ac2_every_refusal_branch_reaches_a_distinct_row(
    case: str, override: dict[str, Any], expected: EnumProdGateOutcome
) -> None:
    """AC2: each branch drives the real gate and lands its own typed code."""
    decision = evaluate_gate(_command(**override))
    assert decision.allowed is False, f"{case} must be a refusal"

    db, result = _project(decision.model_dump(mode="json"))
    assert result["rows_upserted"] == 1

    row = _bound(db)
    assert row["outcome"] == expected.value
    assert row["allowed"] is False
    # The requested digest is on the row for EVERY refusal, including the ones
    # that refuse before any digest is resolved -- a blocked row that cannot
    # say what was being promoted answers none of the questions it exists for.
    assert row["requested_image_digest"] is not None


def test_ac2_a_hardcoded_single_token_writer_would_fail_this() -> None:
    """AC2's own falsifier condition, asserted directly.

    The parametrised test above passes trivially if every case expects the
    same token. This pins that the expectations really are plural, so a writer
    that hardcoded one value could not satisfy the suite.
    """
    distinct = {expected for _, _, expected in _REFUSAL_CASES}
    assert len(distinct) >= 10, (
        "the refusal cases must expect many distinct codes, otherwise a writer "
        f"binding one literal would pass; got {sorted(o.value for o in distinct)}"
    )


def test_every_refusal_branch_in_the_gate_has_a_case_here() -> None:
    """A branch added without a case must fail here, not go unprojected.

    The gate's refusal vocabulary is ``EnumProdGateOutcome`` minus the two
    allow codes and minus ``self_granted``, which OMN-14814 stopped producing.
    A member added to that enum with no case above is a new refusal nobody is
    projecting, which is precisely this ticket's defect returning.
    """
    covered = {expected for _, _, expected in _REFUSAL_CASES}
    allows = {
        EnumProdGateOutcome.ALLOWED,
        EnumProdGateOutcome.ALLOWED_LANE_NOT_GATED,
    }
    unreachable = {EnumProdGateOutcome.SELF_GRANTED}
    # Reachable only by calling evaluate_prod_promotion_gate directly: the
    # COMPUTE node routes a null readiness projection to the same-digest
    # fallback, which refuses with stability_readiness_absent first.
    unreachable_via_node = {
        EnumProdGateOutcome.MISSING_READINESS_PROJECTION,
        EnumProdGateOutcome.DIGEST_NOT_PINNED,
        EnumProdGateOutcome.STABILITY_NOT_READY,
    }
    expected_refusals = (
        set(EnumProdGateOutcome) - allows - unreachable - unreachable_via_node
    )
    assert covered == expected_refusals, (
        "refusal branches with no projection case: "
        f"{sorted(o.value for o in expected_refusals - covered)}; "
        "cases naming a code the enum no longer has: "
        f"{sorted(o.value for o in covered - expected_refusals)}"
    )


def test_the_gate_never_returns_a_decision_without_a_typed_outcome() -> None:
    """The invariant the model deliberately does NOT enforce.

    ``ModelProdPromotionGateDecision.outcome`` is optional, because the model
    is also the redeploy orchestrator's wire model and this topic has carried
    decisions since OMN-13211 that predate the field. A required field would
    fail validation on every retained message the moment this version
    deployed, killing correctly-gated promotions over a field added to
    describe them.

    So the strictness lives here instead. Every branch the gate can reach --
    both allows and every refusal -- is driven through the real gate and
    asserted to carry a code. A new refusal branch returning no outcome fails
    THIS test, which is the same protection with none of the wire cost.
    """
    allows = [
        _command(),
        _command(
            runtime_lane=EnumRuntimeLane.DEV, promotion_grant=None, evaluated_at=None
        ),
    ]
    refusals = [_command(**override) for _, override, _ in _REFUSAL_CASES]

    for command in allows + refusals:
        decision = evaluate_gate(command)
        assert decision.outcome is not None, (
            f"a gate branch returned no typed outcome: {decision.reason!r}. "
            "The projection would record it as 'unknown', which is how this "
            "ticket's defect comes back one branch at a time."
        )
        assert isinstance(decision.outcome, EnumProdGateOutcome)


def test_a_retained_decision_missing_the_outcome_still_validates() -> None:
    """Consumer-first, asserted on the model the ORCHESTRATOR reads.

    This is the exact payload shape recorded off the live topic before
    OMN-18999. If it stops validating, every in-flight promotion fails on
    deploy the moment this version ships.
    """
    from omnimarket.events.runtime_deployment import ModelProdPromotionGateDecision

    retained = {
        "allowed": True,
        "image_digest": None,
        "rollback_target": "omninode-runtime:v2.3.1",
        "reason": "dev lane is not gated; deploy may proceed",
    }
    decision = ModelProdPromotionGateDecision.model_validate(retained)
    assert decision.outcome is None
    assert decision.allowed is True


def test_the_self_granted_reason_is_retained_for_the_wire_and_not_produced() -> None:
    """The measurement behind the deviation from the ticket's "seven".

    ``EnumProdGrantReason`` has seven members, which is where the ticket's
    count comes from. ``self_granted`` is not one the gate can produce:
    OMN-14814 removed dual-control because a solo CODEOWNER cannot satisfy a
    second-approver rule. It is retained for wire stability, and this test
    pins BOTH halves so the retention stays deliberate.
    """
    self_approved = _command(promotion_grant=_grant(approved_by="jonah"))
    decision = evaluate_gate(self_approved)
    assert decision.allowed is True, (
        "a self-approved grant must still authorize (OMN-14814); if this fails, "
        "dual-control came back and the seventh reason is producible again"
    )
    assert EnumProdGateOutcome.SELF_GRANTED.value == "self_granted"


# ---------------------------------------------------------------------------
# AC3 — a permitted promotion is recorded too
# ---------------------------------------------------------------------------


def test_ac3_a_permitted_promotion_also_writes_a_row() -> None:
    """AC3: without this, an allow and a gate that never ran are one absence."""
    decision = evaluate_gate(_command())
    assert decision.allowed is True

    db, result = _project(decision.model_dump(mode="json"))
    assert result["rows_upserted"] == 1

    row = _bound(db)
    assert row["allowed"] is True
    assert row["outcome"] == EnumProdGateOutcome.ALLOWED.value
    assert row["resolved_image_digest"] == STABILITY_DIGEST
    assert row["grant_id"] == GRANT_ID


def test_ac3_a_non_prod_lane_is_recorded_as_ungated_not_as_allowed() -> None:
    """The ungated lanes are a third answer, and they are kept apart.

    A dev deploy is not a prod promotion that passed. Folding them onto one
    token would make "how many promotions did the gate authorize" unanswerable
    from the table it is supposed to be answerable from.
    """
    decision = evaluate_gate(
        _command(
            runtime_lane=EnumRuntimeLane.DEV,
            promotion_grant=None,
            evaluated_at=None,
        )
    )
    db, result = _project(decision.model_dump(mode="json"))
    assert result["rows_upserted"] == 1

    row = _bound(db)
    assert row["allowed"] is True
    assert row["outcome"] == EnumProdGateOutcome.ALLOWED_LANE_NOT_GATED.value
    assert row["evaluated_at"] is None


# ---------------------------------------------------------------------------
# Consumer-first: the reader handles the shape already on the topic
# ---------------------------------------------------------------------------


def test_a_pre_omn18999_decision_still_projects_with_its_token_recovered() -> None:
    """A decision minted before the typed field must still become a row.

    This is what "new wire fields land consumer-first" means mechanically: the
    reader ships able to read the shape already retained on the topic. The
    grant token is RECOVERED from the reason prefix that ``_grant_blocked``
    has always written -- it is read, not guessed -- and the run identity
    falls back to the delivery's deterministic id rather than the row being
    dropped.
    """
    legacy = {
        "allowed": False,
        "image_digest": None,
        "rollback_target": ROLLBACK,
        "reason": (
            "missing_promotion_grant: prod promotion requires an approver-issued "
            "promotion grant; none present"
        ),
    }
    db, result = _project(legacy, offset=41)
    assert result["rows_upserted"] == 1

    row = _bound(db)
    assert row["outcome"] == EnumProdGateOutcome.MISSING_PROMOTION_GRANT.value
    assert row["evaluated_at"] is None
    assert row["grant_id"] is None
    assert isinstance(row["correlation_id"], UUID)


def test_an_unclassifiable_legacy_decision_projects_unknown_not_a_guess() -> None:
    """A refusal with no recoverable token records ``unknown``, honestly.

    The six readiness / digest / evidence branches never carried a token, so
    there is nothing to recover. Naming one would be a fabricated answer that
    reads like a measured one.
    """
    legacy = {
        "allowed": False,
        "reason": "OCC evidence is not durable (pending); prod promotion requires a merged OCC PR",
    }
    db, _ = _project(legacy, offset=7)
    assert _bound(db)["outcome"] == "unknown"


def test_a_field_a_later_producer_adds_does_not_dlq_the_decision() -> None:
    """Tolerance in the other direction, so the reader does not need a lockstep deploy."""
    payload = _decision_payload(_command(promotion_grant=None))
    payload["some_field_a_later_ticket_adds"] = "value"
    db, result = _project(payload)
    assert result["rows_upserted"] == 1
    assert _bound(db)["outcome"] == EnumProdGateOutcome.MISSING_PROMOTION_GRANT.value


# ---------------------------------------------------------------------------
# AC5 — readable from the surface a person uses that day
# ---------------------------------------------------------------------------


def _exposure() -> ProjectionTableConfig:
    """This node's own exposure, loaded from its contract rather than restated."""
    writer = ProdPromotionGateProjectionWriter()
    exposure = writer._snapshot_exposure
    assert exposure is not None, (
        "the contract must declare a bus_backed exposure; without one the row "
        "is durable but unreadable from the status page, which is AC5"
    )
    assert exposure.topic == TOPIC_PROMOTION_GATE, (
        "the exposure the writer publishes and the topic the status page reads "
        "must be the same string, or the panel refuses with unknown_topic"
    )
    return exposure


def _cache_with(exposure: ProjectionTableConfig, row: dict[str, Any]) -> SnapshotCache:
    """A REAL SnapshotCache fed the way the writer feeds it.

    The delta is built by ``encode_snapshot_delta`` -- the same function
    ``BaseProjectionRunner.publish_snapshot_delta`` calls -- so this asserts
    the row the writer would really publish is the row the page can really
    read. A test that hand-wrote the wire shape would stay green if the
    writer's key columns and the cache's expectations drifted apart, which is
    the only interesting way this seam breaks.
    """
    cache = SnapshotCache(
        {exposure.topic: exposure},
        bootstrap_servers="unused:9092",
        group_id="test-omn18999-promotion-gate",
    )
    message = encode_snapshot_delta(
        exposure,
        op="upsert",
        row=row,
        source_event_id=str(row["correlation_id"]),
        source_topic="onex.evt.omnimarket.prod-promotion-gate-evaluated.v1",  # onex-topic-allow: the SOURCE coordinate, not an event_type assignment
        source_partition=0,
        source_offset=1,
        observed_at=datetime.now(UTC).isoformat(),
    )
    assert message is not None, "the exposure must be bus_backed to encode a delta"
    cache.apply_message(
        exposure.topic,
        key=message.key,
        value=message.value,
        headers=[("tenant_id", b"omninode")],
    )
    # No live consumer here, so bootstrap is marked complete directly -- the
    # established pattern for cache-backed tests in this repository.
    cache._state[exposure.topic].bootstrap_complete = True
    return cache


def test_ac5_the_blocked_promotion_reads_back_through_the_status_page_query_path() -> (
    None
):
    """AC5: read back through ``read_projection``, the status page's own path.

    Not a second query written for the test: this is the exact function
    ``build_morning_page`` calls for every panel and the one whose refusal
    taxonomy mirrors ``GET /projection/{topic}``. A row that cannot be
    returned by it is a row an operator cannot see that day.
    """
    command = _command(promotion_grant=_grant(approved_image_digest=OTHER_DIGEST))
    decision = evaluate_gate(command)
    db, _ = _project(decision.model_dump(mode="json"))
    row = _bound(db)

    exposure = _exposure()
    cache = _cache_with(
        exposure,
        {
            "correlation_id": str(row["correlation_id"]),
            "outcome": row["outcome"],
            "allowed": row["allowed"],
            "reason": row["reason"],
            "grant_id": row["grant_id"],
            "requested_image_digest": row["requested_image_digest"],
            "evaluated_at": row["evaluated_at"].isoformat(),
            "projected_at": datetime.now(UTC).isoformat(),
        },
    )

    read = read_projection(
        TOPIC_PROMOTION_GATE,
        {TOPIC_PROMOTION_GATE: exposure},
        cache,
        limit=50,
    )

    assert read.state is EnumPanelState.LIVE, (
        f"the panel refused instead of serving: {read.reason_code} / "
        f"{read.reason_detail}"
    )
    assert len(read.rows) == 1
    served = dict(read.rows[0])
    assert served["outcome"] == EnumProdGateOutcome.GRANT_DIGEST_MISMATCH.value
    assert served["grant_id"] == GRANT_ID
    assert served["requested_image_digest"] == STABILITY_DIGEST
    assert served["evaluated_at"] == EVALUATED_AT.isoformat()


def test_ac5_negative_control_an_empty_cache_refuses_rather_than_reporting_clean() -> (
    None
):
    """The positive read above is only meaningful if the path can also refuse.

    Without this, a ``read_projection`` that returned a canned row for every
    topic would satisfy AC5 while showing an operator nothing.
    """
    exposure = _exposure()
    cache = SnapshotCache(
        {TOPIC_PROMOTION_GATE: exposure},
        bootstrap_servers="unused:9092",
        group_id="test-omn18999-empty",
    )
    read = read_projection(
        TOPIC_PROMOTION_GATE, {TOPIC_PROMOTION_GATE: exposure}, cache, limit=50
    )
    assert read.state is not EnumPanelState.LIVE
    assert read.rows == ()


def test_ac5_the_status_page_reads_this_exposure_as_a_panel() -> None:
    """The exposure has a named renderer, not an opt-out waiting for one.

    ``build_morning_page`` must read this topic. A ``bus_backed`` exposure
    nothing renders is the shape the repository's other projection contracts
    deliberately decline to declare.
    """
    from omnimarket.projection.morning_page import ModelMorningPage, build_morning_page

    assert "promotion_gate" in ModelMorningPage.model_fields

    exposure = _exposure()
    cache = SnapshotCache(
        {TOPIC_PROMOTION_GATE: exposure},
        bootstrap_servers="unused:9092",
        group_id="test-omn18999-panel",
    )
    page = build_morning_page(
        {TOPIC_PROMOTION_GATE: exposure}, cache, service_name="test-lane"
    )
    assert page.promotion_gate.topic == TOPIC_PROMOTION_GATE
    # It refuses here (nothing was published into the cache), and refusing is
    # the correct render. What is asserted is that the panel EXISTS and is
    # bound to this topic -- not that it is green.
    assert page.promotion_gate.state is not EnumPanelState.LIVE


def test_the_degraded_exposure_still_refuses_rather_than_rendering_zero() -> None:
    """A refusal never renders as an empty success on this panel either."""
    exposure = _exposure().model_copy(
        update={"status": ProjectionStatus.DEGRADED, "degraded_reason": "test"}
    )
    read = read_projection(
        TOPIC_PROMOTION_GATE,
        {TOPIC_PROMOTION_GATE: exposure},
        SnapshotCache(
            {TOPIC_PROMOTION_GATE: exposure},
            bootstrap_servers="unused:9092",
            group_id="test-omn18999-degraded",
        ),
        limit=5,
    )
    assert read.state is EnumPanelState.REFUSED
    assert read.reason_code == "contract_degraded"


# ---------------------------------------------------------------------------
# The runtime contract the write path depends on
# ---------------------------------------------------------------------------


def test_the_writer_declares_in_process_runtime_dispatch() -> None:
    """Declared, never inferred from the class name (OMN-16874).

    Undeclared, a runner-shaped class is classified STANDALONE: the shared
    runtime subscribes its topics and dispatches nothing, so the node stores
    nothing while reading healthy on lag and on every watermark. There is no
    dedicated writer Deployment for this node.
    """
    assert ProdPromotionGateProjectionWriter.onex_runtime_inprocess_dispatch is True


def test_handle_reports_the_row_count_key_the_runtime_guard_reads() -> None:
    """A written row must not be scored zero by the runtime write-path guard."""
    _, result = _project(_decision_payload(_command()))
    assert result["rows_upserted"] == 1
    assert len(result["prod_promotion_gate_rows"]) == 1


def test_the_fold_and_the_writer_agree_on_the_message_coordinates() -> None:
    """The source topic recorded on the row is the topic it was read from."""
    writer = ProdPromotionGateProjectionWriter()
    topic = writer.subscribe_topics[0]
    db, _ = _project(_decision_payload(_command()))
    assert _bound(db)["source_topic"] == topic
    assert (
        MessageMeta(partition=0, offset=0, fallback_id="x", topic=topic).topic == topic
    )


def test_a_decision_without_a_correlation_is_keyed_deterministically() -> None:
    """Two deliveries of the SAME message converge; different offsets do not.

    The fallback identity is derived from topic/partition/offset, so it is
    stable for a redelivery and distinct for a genuinely different message.
    Keying every unidentified decision on one constant would collapse the
    whole legacy population onto a single row.
    """
    legacy = {"allowed": False, "reason": "OCC evidence is not durable (pending)"}
    first, _ = _project(dict(legacy), offset=1)
    again, _ = _project(dict(legacy), offset=1)
    other, _ = _project(dict(legacy), offset=2)

    assert _bound(first)["correlation_id"] == _bound(again)["correlation_id"]
    assert _bound(first)["correlation_id"] != _bound(other)["correlation_id"]


def test_the_echoed_correlation_wins_over_the_delivery_fallback() -> None:
    """A run identity is preferred to a delivery coordinate whenever it exists."""
    db, _ = _project(_decision_payload(_command()), offset=99)
    assert _bound(db)["correlation_id"] == CORRELATION


def test_a_fresh_correlation_is_not_silently_shared() -> None:
    """Two different runs are two different rows."""
    other = uuid4()
    db, _ = _project(_decision_payload(_command(correlation_id=other)))
    assert _bound(db)["correlation_id"] == other
