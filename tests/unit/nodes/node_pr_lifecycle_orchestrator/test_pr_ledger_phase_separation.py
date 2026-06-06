# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD coverage for orchestrator phase separation (OMN-12570).

OMN-12569 gave the orchestrator a durable, reconstructable PR ledger. OMN-12570
phase-separates the verification surfaces the ledger records: branch checks,
merge-group checks, and post-merge CI tails are now DISTINCT phases in the
state machine. The two non-negotiable acceptance properties proven here:

1. Phase transitions are EXPLICIT recorded state-machine transitions, captured
   at transition time — not inferred later from logs. The recorded transition
   log survives reconstruction and rejects illegal moves.
2. Ledger entries are attributed to the correct phase, so a POST_MERGE_TAIL
   failure is distinguishable from a BRANCH_CHECKS failure.

Related:
    - OMN-12570: Phase-separate branch / merge-group / post-merge checks.
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
    EnumPrLifecyclePhase,
    InMemoryPrLedgerStore,
    ModelPrLedgerSourceEvent,
    ModelPrLifecyclePhaseTransition,
    ProjectionDatabasePrLedgerStore,
    apply_pr_ledger_event,
    is_allowed_phase_transition,
    reconstruct_pr_ledger,
    record_phase_transition,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

PR_LEDGER_TABLE = "pr_lifecycle_ledger"
RUN_ID = "20260601-180000-phase1"


def _phase_separated_events() -> tuple[ModelPrLedgerSourceEvent, ...]:
    """A deterministic sweep where two PRs fail in DIFFERENT phases.

    * PR 701 fails branch checks (routed to FIX in BRANCH_CHECKS).
    * PR 702 clears branch + merge-group checks, merges, then its post-merge
      CI tail fails (FAILED conclusion recorded in POST_MERGE_TAIL).

    Without phase attribution both look like a bare ``FAILED`` conclusion; the
    point of OMN-12570 is that they are now distinguishable.
    """
    correlation_id = uuid4()
    return (
        # PR 701: inventoried, then fails branch checks.
        ModelPrLedgerSourceEvent(
            kind=EnumPrLedgerEventKind.PR_INVENTORIED,
            run_id=RUN_ID,
            correlation_id=correlation_id,
            repo="OmniNode-ai/omnimarket",
            pr_number=701,
            head_sha="aaa701",
            orchestrator_action=EnumOrchestratorAction.INVENTORY,
            phase=EnumPrLifecyclePhase.INVENTORY,
            observed_at="2026-06-01T18:00:00Z",
        ),
        ModelPrLedgerSourceEvent(
            kind=EnumPrLedgerEventKind.FINAL_CONCLUSION,
            run_id=RUN_ID,
            correlation_id=correlation_id,
            repo="OmniNode-ai/omnimarket",
            pr_number=701,
            head_sha="aaa701",
            conclusion=EnumPrLedgerConclusion.FAILED,
            orchestrator_action=EnumOrchestratorAction.FIX,
            phase=EnumPrLifecyclePhase.BRANCH_CHECKS,
            observed_at="2026-06-01T18:04:00Z",
        ),
        # PR 702: inventoried, merge-group rerun, then post-merge tail fails.
        ModelPrLedgerSourceEvent(
            kind=EnumPrLedgerEventKind.PR_INVENTORIED,
            run_id=RUN_ID,
            correlation_id=correlation_id,
            repo="OmniNode-ai/omnibase_core",
            pr_number=702,
            head_sha="bbb702",
            orchestrator_action=EnumOrchestratorAction.INVENTORY,
            phase=EnumPrLifecyclePhase.INVENTORY,
            observed_at="2026-06-01T18:00:30Z",
        ),
        ModelPrLedgerSourceEvent(
            kind=EnumPrLedgerEventKind.RERUN_ATTEMPTED,
            run_id=RUN_ID,
            correlation_id=correlation_id,
            repo="OmniNode-ai/omnibase_core",
            pr_number=702,
            head_sha="bbb702",
            merge_group_sha="mg-bbb702-2",
            orchestrator_action=EnumOrchestratorAction.REQUEUE,
            phase=EnumPrLifecyclePhase.MERGE_GROUP,
            observed_at="2026-06-01T18:03:00Z",
        ),
        ModelPrLedgerSourceEvent(
            kind=EnumPrLedgerEventKind.FINAL_CONCLUSION,
            run_id=RUN_ID,
            correlation_id=correlation_id,
            repo="OmniNode-ai/omnibase_core",
            pr_number=702,
            head_sha="bbb702",
            merge_group_sha="mg-bbb702-2",
            conclusion=EnumPrLedgerConclusion.FAILED,
            orchestrator_action=EnumOrchestratorAction.MERGE,
            phase=EnumPrLifecyclePhase.POST_MERGE_TAIL,
            observed_at="2026-06-01T18:09:00Z",
        ),
    )


def _recorded_transitions(
    events: tuple[ModelPrLedgerSourceEvent, ...],
) -> tuple[ModelPrLifecyclePhaseTransition, ...]:
    correlation_id = events[0].correlation_id
    return (
        ModelPrLifecyclePhaseTransition(
            run_id=RUN_ID,
            correlation_id=correlation_id,
            from_phase=EnumPrLifecyclePhase.INVENTORY,
            to_phase=EnumPrLifecyclePhase.TRIAGE,
            recorded_at="2026-06-01T18:01:00Z",
        ),
        ModelPrLifecyclePhaseTransition(
            run_id=RUN_ID,
            correlation_id=correlation_id,
            from_phase=EnumPrLifecyclePhase.TRIAGE,
            to_phase=EnumPrLifecyclePhase.BRANCH_CHECKS,
            recorded_at="2026-06-01T18:02:00Z",
        ),
        ModelPrLifecyclePhaseTransition(
            run_id=RUN_ID,
            correlation_id=correlation_id,
            from_phase=EnumPrLifecyclePhase.BRANCH_CHECKS,
            to_phase=EnumPrLifecyclePhase.MERGE_GROUP,
            recorded_at="2026-06-01T18:05:00Z",
        ),
        ModelPrLifecyclePhaseTransition(
            run_id=RUN_ID,
            correlation_id=correlation_id,
            from_phase=EnumPrLifecyclePhase.MERGE_GROUP,
            to_phase=EnumPrLifecyclePhase.POST_MERGE_TAIL,
            recorded_at="2026-06-01T18:08:00Z",
        ),
    )


@pytest.mark.unit
def test_ledger_entries_attributed_to_phase() -> None:
    """Each entry carries the phase its terminal event was recorded in."""
    store = InMemoryPrLedgerStore()
    for event in _phase_separated_events():
        apply_pr_ledger_event(event, store=store)

    ledger = store.load(RUN_ID)
    by_pr = {(e.repo, e.pr_number): e for e in ledger.entries}

    branch_failed = by_pr[("OmniNode-ai/omnimarket", 701)]
    tail_failed = by_pr[("OmniNode-ai/omnibase_core", 702)]

    # Both are FAILED, but in different phases — the OMN-12570 distinction.
    assert branch_failed.conclusion is EnumPrLedgerConclusion.FAILED
    assert tail_failed.conclusion is EnumPrLedgerConclusion.FAILED
    assert branch_failed.last_phase is EnumPrLifecyclePhase.BRANCH_CHECKS
    assert tail_failed.last_phase is EnumPrLifecyclePhase.POST_MERGE_TAIL


@pytest.mark.unit
def test_post_merge_tail_failure_distinguishable_from_branch_failure() -> None:
    """failed_in_phase() separates a tail failure from a branch-check failure."""
    store = InMemoryPrLedgerStore()
    for event in _phase_separated_events():
        apply_pr_ledger_event(event, store=store)

    ledger = store.load(RUN_ID)
    by_pr = {(e.repo, e.pr_number): e for e in ledger.entries}

    assert (
        by_pr[("OmniNode-ai/omnimarket", 701)].failed_in_phase()
        is EnumPrLifecyclePhase.BRANCH_CHECKS
    )
    assert (
        by_pr[("OmniNode-ai/omnibase_core", 702)].failed_in_phase()
        is EnumPrLifecyclePhase.POST_MERGE_TAIL
    )


@pytest.mark.unit
def test_provenance_records_per_event_phase() -> None:
    """Every provenance record retains the phase of its source event."""
    store = InMemoryPrLedgerStore()
    for event in _phase_separated_events():
        apply_pr_ledger_event(event, store=store)

    ledger = store.load(RUN_ID)
    by_pr = {(e.repo, e.pr_number): e for e in ledger.entries}
    pr702 = by_pr[("OmniNode-ai/omnibase_core", 702)]

    phases_by_kind = {p.event_kind: p.phase for p in pr702.provenance}
    assert phases_by_kind[EnumPrLedgerEventKind.PR_INVENTORIED] is (
        EnumPrLifecyclePhase.INVENTORY
    )
    assert phases_by_kind[EnumPrLedgerEventKind.RERUN_ATTEMPTED] is (
        EnumPrLifecyclePhase.MERGE_GROUP
    )
    assert phases_by_kind[EnumPrLedgerEventKind.FINAL_CONCLUSION] is (
        EnumPrLifecyclePhase.POST_MERGE_TAIL
    )


@pytest.mark.unit
def test_explicit_transitions_recorded_at_transition_time() -> None:
    """Phase transitions are recorded in the ledger, not inferred from logs."""
    store = InMemoryPrLedgerStore()
    events = _phase_separated_events()
    for event in events:
        apply_pr_ledger_event(event, store=store)
    for transition in _recorded_transitions(events):
        record_phase_transition(transition, store=store)

    ledger = store.load(RUN_ID)
    recorded = tuple((t.from_phase, t.to_phase) for t in ledger.phase_transitions)
    assert recorded == (
        (EnumPrLifecyclePhase.INVENTORY, EnumPrLifecyclePhase.TRIAGE),
        (EnumPrLifecyclePhase.TRIAGE, EnumPrLifecyclePhase.BRANCH_CHECKS),
        (EnumPrLifecyclePhase.BRANCH_CHECKS, EnumPrLifecyclePhase.MERGE_GROUP),
        (EnumPrLifecyclePhase.MERGE_GROUP, EnumPrLifecyclePhase.POST_MERGE_TAIL),
    )


@pytest.mark.unit
def test_illegal_phase_transition_is_rejected() -> None:
    """Recording an undeclared transition raises — the log stays consistent."""
    store = InMemoryPrLedgerStore()
    bad = ModelPrLifecyclePhaseTransition(
        run_id=RUN_ID,
        correlation_id=uuid4(),
        # Jumping straight from branch checks to post-merge tail skips the
        # merge-group phase — an impossible state-machine path.
        from_phase=EnumPrLifecyclePhase.BRANCH_CHECKS,
        to_phase=EnumPrLifecyclePhase.POST_MERGE_TAIL,
        recorded_at="2026-06-01T18:06:00Z",
    )
    with pytest.raises(ValueError, match="illegal phase transition"):
        record_phase_transition(bad, store=store)
    # Nothing was recorded.
    assert store.load(RUN_ID).phase_transitions == ()


@pytest.mark.unit
def test_is_allowed_phase_transition_table() -> None:
    """The declared transition table matches the three-phase CI pipeline."""
    phase = EnumPrLifecyclePhase
    assert is_allowed_phase_transition(phase.TRIAGE, phase.BRANCH_CHECKS)
    assert is_allowed_phase_transition(phase.BRANCH_CHECKS, phase.MERGE_GROUP)
    assert is_allowed_phase_transition(phase.MERGE_GROUP, phase.POST_MERGE_TAIL)
    # A green PR enqueues straight from triage into the merge group.
    assert is_allowed_phase_transition(phase.TRIAGE, phase.MERGE_GROUP)
    # A run that both merges and fixes does the merge tail first, then fixes.
    assert is_allowed_phase_transition(phase.POST_MERGE_TAIL, phase.BRANCH_CHECKS)
    # Self-transition (e.g. a merge-group rerun) is always legal.
    assert is_allowed_phase_transition(phase.MERGE_GROUP, phase.MERGE_GROUP)
    # Skipping merge-group from branch checks straight to the tail is not.
    assert not is_allowed_phase_transition(phase.BRANCH_CHECKS, phase.POST_MERGE_TAIL)
    # Moving backward (tail back to the merge group) is not.
    assert not is_allowed_phase_transition(phase.POST_MERGE_TAIL, phase.MERGE_GROUP)
    # Re-triaging after merging is not a legal move.
    assert not is_allowed_phase_transition(phase.MERGE_GROUP, phase.TRIAGE)


@pytest.mark.unit
def test_reconstruct_preserves_phase_and_transition_log() -> None:
    """Reconstruction reproduces phase attribution + the transition log.

    Both events and transitions carry an explicit phase, so a shuffled durable
    log rebuilds the exact same projection — nothing is inferred.
    """
    events = _phase_separated_events()
    transitions = _recorded_transitions(events)

    live = InMemoryPrLedgerStore()
    for event in events:
        apply_pr_ledger_event(event, store=live)
    for transition in transitions:
        record_phase_transition(transition, store=live)
    incremental = live.load(RUN_ID)

    shuffled_events = (events[4], events[0], events[3], events[1], events[2])
    shuffled_transitions = (
        transitions[2],
        transitions[0],
        transitions[3],
        transitions[1],
    )
    rebuilt = reconstruct_pr_ledger(
        shuffled_events, phase_transitions=shuffled_transitions
    )

    assert rebuilt.model_dump(mode="json") == incremental.model_dump(mode="json")


@pytest.mark.unit
def test_projection_database_store_persists_transitions() -> None:
    """The durable projection store round-trips the recorded transition log."""
    db = InmemoryDatabaseAdapter()
    store = ProjectionDatabasePrLedgerStore(db, table=PR_LEDGER_TABLE)
    events = _phase_separated_events()
    transitions = _recorded_transitions(events)
    for event in events:
        apply_pr_ledger_event(event, store=store)
    for transition in transitions:
        record_phase_transition(transition, store=store)

    # Transitions land in the sibling table keyed independently of entries.
    transition_rows = db.query(f"{PR_LEDGER_TABLE}_transitions")
    assert len(transition_rows) == len(transitions)

    reloaded = store.load(RUN_ID)
    assert tuple((t.from_phase, t.to_phase) for t in reloaded.phase_transitions) == (
        (EnumPrLifecyclePhase.INVENTORY, EnumPrLifecyclePhase.TRIAGE),
        (EnumPrLifecyclePhase.TRIAGE, EnumPrLifecyclePhase.BRANCH_CHECKS),
        (EnumPrLifecyclePhase.BRANCH_CHECKS, EnumPrLifecyclePhase.MERGE_GROUP),
        (EnumPrLifecyclePhase.MERGE_GROUP, EnumPrLifecyclePhase.POST_MERGE_TAIL),
    )
    # Entry phase attribution survived the durable round-trip.
    by_pr = {(e.repo, e.pr_number): e for e in reloaded.entries}
    assert (
        by_pr[("OmniNode-ai/omnibase_core", 702)].failed_in_phase()
        is EnumPrLifecyclePhase.POST_MERGE_TAIL
    )
