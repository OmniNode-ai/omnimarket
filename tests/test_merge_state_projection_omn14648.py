# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the WS6 merge-state telemetry projection (OMN-14648).

Covers the three deliverables of the REPORT-ONLY first PR:

1. The typed state-transition event model — deterministic fingerprint identity,
   FSM-transition validation (reusing EnumPrLifecyclePhase), and rerun reason
   codes.
2. The projector — idempotent UPSERT deduped by the deterministic event_id.
3. The measurement layer — merge-flow metrics folded from the event log:
   per-state duration, evidence-volume ratio (baseline 1.67 -> target <=1.1),
   companions per product PR, same-head reruns by reason code, queue wait, and
   product failures found before vs after evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.events.merge_state import (
    EnumMergeRerunReason,
    ModelMergeStateTransitionEvent,
)
from omnimarket.nodes.merge_state_metrics_native import (
    EVIDENCE_VOLUME_RATIO_BASELINE,
    EVIDENCE_VOLUME_RATIO_TARGET,
    compute_merge_flow_metrics,
)
from omnimarket.nodes.node_merge_state_projection.handlers.handler_merge_state_projection import (
    HandlerMergeStateProjection,
)
from omnimarket.nodes.pr_ledger_native import EnumPrLifecyclePhase
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_T0 = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


def _txn(
    *,
    repo: str = "omnimarket",
    pr_number: int = 100,
    head_sha: str = "deadbeef",
    from_state: EnumPrLifecyclePhase,
    to_state: EnumPrLifecyclePhase,
    at: datetime,
    reason_code: EnumMergeRerunReason | None = None,
    is_occ_evidence: bool = False,
    product_pr_number: int | None = None,
    queue_wait_seconds: float | None = None,
    product_failure_found: bool = False,
    evidence_present: bool = False,
) -> ModelMergeStateTransitionEvent:
    return ModelMergeStateTransitionEvent(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        from_state=from_state,
        to_state=to_state,
        occurred_at=at,
        reason_code=reason_code,
        is_occ_evidence=is_occ_evidence,
        product_pr_number=product_pr_number,
        queue_wait_seconds=queue_wait_seconds,
        product_failure_found=product_failure_found,
        evidence_present=evidence_present,
    )


# --------------------------------------------------------------------------- #
# 1. Event model
# --------------------------------------------------------------------------- #


def test_event_id_is_deterministic_and_stable() -> None:
    a = _txn(
        from_state=EnumPrLifecyclePhase.TRIAGE,
        to_state=EnumPrLifecyclePhase.MERGE_GROUP,
        at=_T0,
    )
    b = _txn(
        from_state=EnumPrLifecyclePhase.TRIAGE,
        to_state=EnumPrLifecyclePhase.MERGE_GROUP,
        at=_T0,
    )
    assert a.event_id == b.event_id
    assert len(a.event_id) == 16
    assert a.event_id == a.event_id  # stable across access


def test_event_id_differs_when_identity_differs() -> None:
    base = _txn(
        from_state=EnumPrLifecyclePhase.TRIAGE,
        to_state=EnumPrLifecyclePhase.MERGE_GROUP,
        at=_T0,
    )
    later = _txn(
        from_state=EnumPrLifecyclePhase.TRIAGE,
        to_state=EnumPrLifecyclePhase.MERGE_GROUP,
        at=_T0 + timedelta(seconds=1),
    )
    other_pr = _txn(
        pr_number=101,
        from_state=EnumPrLifecyclePhase.TRIAGE,
        to_state=EnumPrLifecyclePhase.MERGE_GROUP,
        at=_T0,
    )
    assert base.event_id != later.event_id
    assert base.event_id != other_pr.event_id


def test_illegal_transition_is_rejected() -> None:
    # INVENTORY -> MERGE_GROUP is not a declared FSM edge.
    with pytest.raises(ValueError, match="illegal merge-flow transition"):
        _txn(
            from_state=EnumPrLifecyclePhase.INVENTORY,
            to_state=EnumPrLifecyclePhase.MERGE_GROUP,
            at=_T0,
        )


def test_self_transition_is_allowed() -> None:
    # A merge-group rerun (self-transition) is legal.
    evt = _txn(
        from_state=EnumPrLifecyclePhase.MERGE_GROUP,
        to_state=EnumPrLifecyclePhase.MERGE_GROUP,
        at=_T0,
        reason_code=EnumMergeRerunReason.MERGE_GROUP_TIMEOUT,
    )
    assert evt.reason_code is EnumMergeRerunReason.MERGE_GROUP_TIMEOUT


def test_event_model_forbids_extra_fields() -> None:
    with pytest.raises(ValueError, match=r"[Ee]xtra"):
        ModelMergeStateTransitionEvent(
            repo="omnimarket",
            pr_number=1,
            head_sha="abc",
            from_state=EnumPrLifecyclePhase.TRIAGE,
            to_state=EnumPrLifecyclePhase.MERGE_GROUP,
            occurred_at=_T0,
            bogus="nope",  # type: ignore[call-arg]
        )


# --------------------------------------------------------------------------- #
# 2. Projector
# --------------------------------------------------------------------------- #


def test_projector_upserts_one_row() -> None:
    db = InmemoryDatabaseAdapter()
    handler = HandlerMergeStateProjection()
    evt = _txn(
        from_state=EnumPrLifecyclePhase.TRIAGE,
        to_state=EnumPrLifecyclePhase.MERGE_GROUP,
        at=_T0,
    )
    result = handler.project(evt, db)
    assert result.rows_upserted == 1
    rows = db.tables["merge_state_transitions"]
    assert len(rows) == 1
    assert rows[0]["event_id"] == evt.event_id
    assert rows[0]["from_state"] == "triage"
    assert rows[0]["to_state"] == "merge_group"


def test_projector_is_idempotent_under_replay() -> None:
    db = InmemoryDatabaseAdapter()
    handler = HandlerMergeStateProjection()
    evt = _txn(
        from_state=EnumPrLifecyclePhase.TRIAGE,
        to_state=EnumPrLifecyclePhase.MERGE_GROUP,
        at=_T0,
    )
    handler.project(evt, db)
    handler.project(evt, db)  # replay same transition
    assert len(db.tables["merge_state_transitions"]) == 1


def test_handle_shim_recomputes_event_id_and_requires_adapter() -> None:
    db = InmemoryDatabaseAdapter()
    handler = HandlerMergeStateProjection()
    evt = _txn(
        from_state=EnumPrLifecyclePhase.TRIAGE,
        to_state=EnumPrLifecyclePhase.MERGE_GROUP,
        at=_T0,
    )
    payload = evt.model_dump(mode="json")
    payload["event_id"] = "forged-value"  # must be recomputed, not trusted
    payload["_db"] = db
    out = handler.handle(payload)
    assert out["rows_upserted"] == 1
    assert db.tables["merge_state_transitions"][0]["event_id"] == evt.event_id

    with pytest.raises(TypeError, match="DatabaseAdapter"):
        handler.handle(evt.model_dump(mode="json"))


# --------------------------------------------------------------------------- #
# 3. Metrics — the measurement layer
# --------------------------------------------------------------------------- #


def test_empty_window_yields_zero_metrics() -> None:
    m = compute_merge_flow_metrics([])
    assert m.transitions_observed == 0
    assert m.evidence_volume_ratio is None
    assert m.companions_per_product_pr is None
    assert m.evidence_volume_meets_target is False


def test_per_state_duration() -> None:
    # One PR: TRIAGE (entered at T0) -> BRANCH_CHECKS at T0+30s -> MERGE_GROUP at
    # T0+90s. Time in BRANCH_CHECKS = 60s.
    txns = [
        _txn(
            from_state=EnumPrLifecyclePhase.INVENTORY,
            to_state=EnumPrLifecyclePhase.TRIAGE,
            at=_T0,
        ),
        _txn(
            from_state=EnumPrLifecyclePhase.TRIAGE,
            to_state=EnumPrLifecyclePhase.BRANCH_CHECKS,
            at=_T0 + timedelta(seconds=30),
        ),
        _txn(
            from_state=EnumPrLifecyclePhase.BRANCH_CHECKS,
            to_state=EnumPrLifecyclePhase.MERGE_GROUP,
            at=_T0 + timedelta(seconds=90),
        ),
    ]
    m = compute_merge_flow_metrics(txns)
    assert m.mean_duration_seconds_per_state["triage"] == pytest.approx(30.0)
    assert m.mean_duration_seconds_per_state["branch_checks"] == pytest.approx(60.0)


def test_evidence_volume_ratio_baseline_and_target() -> None:
    # Baseline reproduction: 5 OCC-evidence merges + 3 product merges => 1.667.
    txns: list[ModelMergeStateTransitionEvent] = []
    for i in range(3):
        txns.append(
            _txn(
                pr_number=200 + i,
                head_sha=f"prod{i}",
                from_state=EnumPrLifecyclePhase.MERGE_GROUP,
                to_state=EnumPrLifecyclePhase.TERMINAL,
                at=_T0 + timedelta(minutes=i),
                is_occ_evidence=False,
            )
        )
    for i in range(5):
        txns.append(
            _txn(
                pr_number=300 + i,
                head_sha=f"occ{i}",
                from_state=EnumPrLifecyclePhase.MERGE_GROUP,
                to_state=EnumPrLifecyclePhase.TERMINAL,
                at=_T0 + timedelta(minutes=10 + i),
                is_occ_evidence=True,
            )
        )
    m = compute_merge_flow_metrics(txns)
    assert m.occ_evidence_merges == 5
    assert m.product_merges == 3
    assert m.evidence_volume_ratio == pytest.approx(
        EVIDENCE_VOLUME_RATIO_BASELINE, abs=0.01
    )
    assert m.evidence_volume_meets_target is False

    # Target-meeting window: 2 OCC-evidence + 2 product => ratio 1.0 <= 1.1.
    good = [
        _txn(
            pr_number=400,
            head_sha="p0",
            from_state=EnumPrLifecyclePhase.MERGE_GROUP,
            to_state=EnumPrLifecyclePhase.TERMINAL,
            at=_T0,
            is_occ_evidence=False,
        ),
        _txn(
            pr_number=401,
            head_sha="p1",
            from_state=EnumPrLifecyclePhase.MERGE_GROUP,
            to_state=EnumPrLifecyclePhase.TERMINAL,
            at=_T0,
            is_occ_evidence=False,
        ),
        _txn(
            pr_number=500,
            head_sha="e0",
            from_state=EnumPrLifecyclePhase.MERGE_GROUP,
            to_state=EnumPrLifecyclePhase.TERMINAL,
            at=_T0,
            is_occ_evidence=True,
        ),
        _txn(
            pr_number=501,
            head_sha="e1",
            from_state=EnumPrLifecyclePhase.MERGE_GROUP,
            to_state=EnumPrLifecyclePhase.TERMINAL,
            at=_T0,
            is_occ_evidence=True,
        ),
    ]
    mg = compute_merge_flow_metrics(good)
    assert mg.evidence_volume_ratio == pytest.approx(1.0)
    assert mg.evidence_volume_meets_target is True
    assert mg.evidence_volume_ratio_target == EVIDENCE_VOLUME_RATIO_TARGET


def test_companions_per_product_pr() -> None:
    # Product PR 600 merges; two OCC companions (701, 702) bind to it.
    txns = [
        _txn(
            pr_number=600,
            head_sha="prod",
            from_state=EnumPrLifecyclePhase.MERGE_GROUP,
            to_state=EnumPrLifecyclePhase.TERMINAL,
            at=_T0,
            is_occ_evidence=False,
        ),
        _txn(
            pr_number=701,
            head_sha="c1",
            from_state=EnumPrLifecyclePhase.TRIAGE,
            to_state=EnumPrLifecyclePhase.MERGE_GROUP,
            at=_T0,
            is_occ_evidence=True,
            product_pr_number=600,
        ),
        _txn(
            pr_number=702,
            head_sha="c2",
            from_state=EnumPrLifecyclePhase.TRIAGE,
            to_state=EnumPrLifecyclePhase.MERGE_GROUP,
            at=_T0,
            is_occ_evidence=True,
            product_pr_number=600,
        ),
    ]
    m = compute_merge_flow_metrics(txns)
    assert m.companions_per_product_pr == pytest.approx(2.0)


def test_same_head_reruns_by_reason() -> None:
    txns = [
        _txn(
            from_state=EnumPrLifecyclePhase.MERGE_GROUP,
            to_state=EnumPrLifecyclePhase.MERGE_GROUP,
            at=_T0,
            reason_code=EnumMergeRerunReason.STALE_OCC_PREFLIGHT,
        ),
        _txn(
            from_state=EnumPrLifecyclePhase.MERGE_GROUP,
            to_state=EnumPrLifecyclePhase.MERGE_GROUP,
            at=_T0 + timedelta(seconds=1),
            reason_code=EnumMergeRerunReason.STALE_OCC_PREFLIGHT,
        ),
        _txn(
            from_state=EnumPrLifecyclePhase.BRANCH_CHECKS,
            to_state=EnumPrLifecyclePhase.BRANCH_CHECKS,
            at=_T0 + timedelta(seconds=2),
            reason_code=EnumMergeRerunReason.CODERABBIT_UNRESOLVED,
        ),
    ]
    m = compute_merge_flow_metrics(txns)
    assert m.same_head_reruns_by_reason == {
        "stale_occ_preflight": 2,
        "coderabbit_unresolved": 1,
    }


def test_queue_wait() -> None:
    txns = [
        _txn(
            pr_number=800,
            from_state=EnumPrLifecyclePhase.TRIAGE,
            to_state=EnumPrLifecyclePhase.MERGE_GROUP,
            at=_T0,
            queue_wait_seconds=120.0,
        ),
        _txn(
            pr_number=801,
            from_state=EnumPrLifecyclePhase.TRIAGE,
            to_state=EnumPrLifecyclePhase.MERGE_GROUP,
            at=_T0,
            queue_wait_seconds=240.0,
        ),
    ]
    m = compute_merge_flow_metrics(txns)
    assert m.queue_wait_seconds_total == pytest.approx(360.0)
    assert m.queue_wait_seconds_p50 == pytest.approx(180.0)


def test_product_failures_before_vs_after_evidence() -> None:
    txns = [
        _txn(
            pr_number=900,
            from_state=EnumPrLifecyclePhase.BRANCH_CHECKS,
            to_state=EnumPrLifecyclePhase.TERMINAL,
            at=_T0,
            product_failure_found=True,
            evidence_present=False,
        ),
        _txn(
            pr_number=901,
            from_state=EnumPrLifecyclePhase.POST_MERGE_TAIL,
            to_state=EnumPrLifecyclePhase.TERMINAL,
            at=_T0,
            product_failure_found=True,
            evidence_present=True,
        ),
        _txn(
            pr_number=902,
            from_state=EnumPrLifecyclePhase.POST_MERGE_TAIL,
            to_state=EnumPrLifecyclePhase.TERMINAL,
            at=_T0,
            product_failure_found=True,
            evidence_present=True,
        ),
    ]
    m = compute_merge_flow_metrics(txns)
    assert m.product_failures_before_evidence == 1
    assert m.product_failures_after_evidence == 2
