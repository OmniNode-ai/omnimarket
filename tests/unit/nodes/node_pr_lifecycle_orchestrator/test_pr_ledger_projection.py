# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD coverage for the durable, reconstructable PR ledger (OMN-12569).

The ledger is a *derived projection* from GitHub events / workflow runs /
merge-group state / orchestrator actions. It is NOT authoritative truth by
itself — authoritative truth remains GitHub state + durable orchestrator
receipts. These tests prove two non-negotiable properties:

1. A simulated sweep materializes ledger entries with all required fields and
   per-entry provenance (workflow run, merge-group SHA, branch SHA,
   orchestrator action, timestamp).
2. Reconstruction from the same source events + receipts yields the identical
   ledger state — i.e. the projection is reconstructable, not merely
   persistent.

Related:
    - OMN-12569: Orchestrator owns a durable, reconstructable PR ledger.
    - OMN-12504: Merge queue recovery epic.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.pr_ledger_native import (
    EnumOrchestratorAction,
    EnumPrLedgerConclusion,
    EnumPrLedgerEventKind,
    InMemoryPrLedgerStore,
    ModelPrLedgerSourceEvent,
    ProjectionDatabasePrLedgerStore,
    apply_pr_ledger_event,
    reconstruct_pr_ledger,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

PR_LEDGER_TABLE = "pr_lifecycle_ledger"


def _simulated_sweep_events() -> tuple[ModelPrLedgerSourceEvent, ...]:
    """A deterministic, ordered stream of source events for one sweep run.

    Models the real orchestrator lifecycle for two PRs across one queue cycle:
    inventoried -> workflow run observed -> merge-group SHA minted ->
    rerun attempt -> final conclusion.
    """
    run_id = "20260601-120000-abc123"
    correlation_id = uuid4()
    return (
        # PR 901: inventoried, one workflow run, queued, merged cleanly.
        ModelPrLedgerSourceEvent(
            kind=EnumPrLedgerEventKind.PR_INVENTORIED,
            run_id=run_id,
            correlation_id=correlation_id,
            repo="OmniNode-ai/omnimarket",
            pr_number=901,
            head_sha="aaa111",
            orchestrator_action=EnumOrchestratorAction.INVENTORY,
            observed_at="2026-06-01T12:00:00Z",
        ),
        ModelPrLedgerSourceEvent(
            kind=EnumPrLedgerEventKind.WORKFLOW_RUN_OBSERVED,
            run_id=run_id,
            correlation_id=correlation_id,
            repo="OmniNode-ai/omnimarket",
            pr_number=901,
            head_sha="aaa111",
            workflow_run_id=555001,
            orchestrator_action=EnumOrchestratorAction.OBSERVE,
            observed_at="2026-06-01T12:01:00Z",
        ),
        ModelPrLedgerSourceEvent(
            kind=EnumPrLedgerEventKind.MERGE_GROUP_SHA_MINTED,
            run_id=run_id,
            correlation_id=correlation_id,
            repo="OmniNode-ai/omnimarket",
            pr_number=901,
            head_sha="aaa111",
            merge_group_sha="mg-aaa111-1",
            orchestrator_action=EnumOrchestratorAction.ENQUEUE,
            observed_at="2026-06-01T12:02:00Z",
        ),
        ModelPrLedgerSourceEvent(
            kind=EnumPrLedgerEventKind.FINAL_CONCLUSION,
            run_id=run_id,
            correlation_id=correlation_id,
            repo="OmniNode-ai/omnimarket",
            pr_number=901,
            head_sha="aaa111",
            merge_group_sha="mg-aaa111-1",
            conclusion=EnumPrLedgerConclusion.MERGED,
            orchestrator_action=EnumOrchestratorAction.MERGE,
            observed_at="2026-06-01T12:05:00Z",
        ),
        # PR 902: inventoried, stalled in queue, reran once, then failed.
        ModelPrLedgerSourceEvent(
            kind=EnumPrLedgerEventKind.PR_INVENTORIED,
            run_id=run_id,
            correlation_id=correlation_id,
            repo="OmniNode-ai/omnibase_core",
            pr_number=902,
            head_sha="bbb222",
            orchestrator_action=EnumOrchestratorAction.INVENTORY,
            observed_at="2026-06-01T12:00:30Z",
        ),
        ModelPrLedgerSourceEvent(
            kind=EnumPrLedgerEventKind.MERGE_GROUP_SHA_MINTED,
            run_id=run_id,
            correlation_id=correlation_id,
            repo="OmniNode-ai/omnibase_core",
            pr_number=902,
            head_sha="bbb222",
            merge_group_sha="mg-bbb222-1",
            orchestrator_action=EnumOrchestratorAction.ENQUEUE,
            observed_at="2026-06-01T12:03:00Z",
        ),
        # Stall remediation: dequeue + re-enqueue mints a fresh merge-group SHA
        # and bumps the rerun attempt counter.
        ModelPrLedgerSourceEvent(
            kind=EnumPrLedgerEventKind.RERUN_ATTEMPTED,
            run_id=run_id,
            correlation_id=correlation_id,
            repo="OmniNode-ai/omnibase_core",
            pr_number=902,
            head_sha="bbb222",
            merge_group_sha="mg-bbb222-2",
            orchestrator_action=EnumOrchestratorAction.REQUEUE,
            observed_at="2026-06-01T12:20:00Z",
        ),
        ModelPrLedgerSourceEvent(
            kind=EnumPrLedgerEventKind.FINAL_CONCLUSION,
            run_id=run_id,
            correlation_id=correlation_id,
            repo="OmniNode-ai/omnibase_core",
            pr_number=902,
            head_sha="bbb222",
            merge_group_sha="mg-bbb222-2",
            conclusion=EnumPrLedgerConclusion.FAILED,
            orchestrator_action=EnumOrchestratorAction.FIX,
            observed_at="2026-06-01T12:25:00Z",
        ),
    )


@pytest.mark.unit
def test_simulated_sweep_populates_ledger_with_provenance() -> None:
    """A simulated sweep materializes ledger entries with full provenance."""
    store = InMemoryPrLedgerStore()
    for event in _simulated_sweep_events():
        apply_pr_ledger_event(event, store=store)

    ledger = store.load("20260601-120000-abc123")
    by_pr = {(e.repo, e.pr_number): e for e in ledger.entries}
    assert set(by_pr) == {
        ("OmniNode-ai/omnimarket", 901),
        ("OmniNode-ai/omnibase_core", 902),
    }

    merged = by_pr[("OmniNode-ai/omnimarket", 901)]
    # All required ledger fields populated.
    assert merged.run_id == "20260601-120000-abc123"
    assert merged.head_sha == "aaa111"
    assert merged.merge_group_shas == ("mg-aaa111-1",)
    assert merged.rerun_attempts == 0
    assert merged.conclusion is EnumPrLedgerConclusion.MERGED
    assert merged.workflow_run_ids == (555001,)
    # Per-entry provenance: every recorded source event is retained with
    # workflow run, merge-group SHA, branch SHA, orchestrator action, timestamp.
    assert merged.provenance, "provenance must be populated"
    last = merged.provenance[-1]
    assert last.orchestrator_action is EnumOrchestratorAction.MERGE
    assert last.merge_group_sha == "mg-aaa111-1"
    assert last.branch_sha == "aaa111"
    assert last.observed_at == "2026-06-01T12:05:00Z"
    assert last.workflow_run is not None or last.merge_group_sha is not None

    failed = by_pr[("OmniNode-ai/omnibase_core", 902)]
    assert failed.rerun_attempts == 1
    assert failed.merge_group_shas == ("mg-bbb222-1", "mg-bbb222-2")
    assert failed.conclusion is EnumPrLedgerConclusion.FAILED

    # Ledger declares freshness provenance metadata (DT-003 / DT-005).
    assert ledger.last_event_at == "2026-06-01T12:25:00Z"
    assert ledger.provenance_kind == "derived_projection"


@pytest.mark.unit
def test_reconstruct_yields_identical_state() -> None:
    """Reconstruction from source events + receipts reproduces the same ledger.

    Two independent paths to the projection must converge byte-for-byte:
      - incremental: fold each event into a live store (the sweep path)
      - reconstruct: replay the full event stream from scratch
    Equality of the serialized projections proves the ledger is a reconstructable
    derived projection, not a manually-trusted state store.
    """
    events = _simulated_sweep_events()

    live = InMemoryPrLedgerStore()
    for event in events:
        apply_pr_ledger_event(event, store=live)
    incremental = live.load("20260601-120000-abc123")

    rebuilt = reconstruct_pr_ledger(events)

    assert rebuilt.model_dump(mode="json") == incremental.model_dump(mode="json")


@pytest.mark.unit
def test_reconstruct_is_order_independent_for_distinct_prs() -> None:
    """Per-PR fold order does not change the final projection.

    Reconstruction sorts source events deterministically by (repo, pr_number,
    observed_at) so a shuffled durable log rebuilds the same state — the
    property the doctrine requires of a reconstructable projection.
    """
    events = _simulated_sweep_events()
    shuffled = (
        events[4],
        events[0],
        events[7],
        events[2],
        events[1],
        events[5],
        events[3],
        events[6],
    )

    canonical = reconstruct_pr_ledger(events)
    from_shuffled = reconstruct_pr_ledger(shuffled)

    assert from_shuffled.model_dump(mode="json") == canonical.model_dump(mode="json")


@pytest.mark.unit
def test_projection_database_store_round_trips_through_durable_surface() -> None:
    """The control-plane durable store persists and reloads identical state.

    Proves the ledger lives in a control-plane durable store (the projection
    database boundary), not a repo artifact, while staying reconstructable.
    """
    db = InmemoryDatabaseAdapter()
    store = ProjectionDatabasePrLedgerStore(db, table=PR_LEDGER_TABLE)
    events = _simulated_sweep_events()
    for event in events:
        apply_pr_ledger_event(event, store=store)

    # Rows landed in the durable surface keyed by (run_id, repo, pr_number).
    rows = db.query(PR_LEDGER_TABLE)
    assert len(rows) == 2
    assert all(row.get("run_id") == "20260601-120000-abc123" for row in rows)

    reloaded = store.load("20260601-120000-abc123")
    rebuilt = reconstruct_pr_ledger(events)
    assert reloaded.model_dump(mode="json") == rebuilt.model_dump(mode="json")
