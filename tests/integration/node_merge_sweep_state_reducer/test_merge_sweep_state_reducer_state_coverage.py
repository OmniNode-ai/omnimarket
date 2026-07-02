# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared-state coverage for node_merge_sweep_state_reducer (OMN-13674).

REDUCER archetype -> Variant B: the reducer's ``handle`` shim is registered on
the canonical in-memory bus (``EventBusInmemory`` via ``integration_event_bus``)
through ``LocalRuntimeBusAdapter`` (``drive_round_trip``). A classified sweep
outcome is published on the ``pr-polish-outcome.v1`` subscribe topic and the
reduced aggregate state republished on the ``merge-sweep-state-reduced.v1`` topic
is asserted.

This suite closes the ``state_machine`` declared-state set from ``contract.yaml``
(``pending -> {armed|rebased|ci_rerun_triggered} -> {merged|failed|stuck}``) by
asserting the *literal* lowercase per-PR outcome label each fold lands in
(``pending`` = a PR with no recorded outcome yet), plus the REDUCER dedup /
exactly-once dimensions:

  * first-writer-wins dedup on a duplicate classified outcome (idempotency),
  * exactly-once terminal ``merge-sweep-completed.v1`` emission when all PRs are
    accounted for, and
  * the Phase 2 polish-completion folds (thread-reply / conflict / ci-fix).

No live Kafka / .201 — fully in-process.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_ci_fix_effect.models.model_ci_fix_result import CiFixResult
from omnimarket.nodes.node_merge_sweep_state_reducer.handlers.handler_sweep_state import (
    TOPIC_SWEEP_COMPLETED,
    HandlerMergeSweepStateReducer,
)
from omnimarket.nodes.node_merge_sweep_state_reducer.models.model_merge_sweep_state import (
    ModelMergeSweepState,
)
from omnimarket.nodes.node_merge_sweep_state_reducer.models.model_phase2_events import (
    ModelConflictResolvedEvent,
)
from omnimarket.nodes.node_sweep_outcome_classify.models.model_sweep_outcome import (
    EnumSweepOutcome,
    ModelSweepOutcomeClassified,
)
from omnimarket.nodes.node_thread_reply_effect.models.model_thread_replied_event import (
    ModelThreadRepliedEvent,
)

_START_TOPIC = "onex.evt.omnimarket.pr-polish-outcome.v1"
_RESULT_TOPIC = "onex.evt.omnimarket.merge-sweep-state-reduced.v1"

_REPO = "OmniNode-ai/omnimarket"


def _classified(
    pr_number: int,
    outcome: EnumSweepOutcome,
    *,
    total_prs: int,
    run_id: UUID,
    correlation_id: UUID,
) -> ModelSweepOutcomeClassified:
    return ModelSweepOutcomeClassified(
        pr_number=pr_number,
        repo=_REPO,
        correlation_id=correlation_id,
        run_id=run_id,
        total_prs=total_prs,
        outcome=outcome,
        source_event_type="pr_polish_completed",
    )


def _outcome_label(state: dict[str, Any], key: str) -> str:
    """Return a PR's recorded outcome label, or ``pending`` when unclassified.

    Models the FSM ``pending`` initial state: a PR the sweep has seen but for
    which no classified outcome has been folded yet.
    """
    record = state["pr_outcomes_by_key"].get(key)
    return record["outcome"] if record is not None else "pending"


async def _drive(
    event: ModelSweepOutcomeClassified, bus: Any, *, group: str
) -> dict[str, Any]:
    """Publish one classified outcome over the bus; return the reduced result dict."""
    from tests.integration._wave7_bus import drive_round_trip

    history = await drive_round_trip(
        bus,
        handler=HandlerMergeSweepStateReducer(),
        handler_name="merge-sweep-state-reducer",
        input_model_cls=ModelSweepOutcomeClassified,
        start_topic=f"{_START_TOPIC}.{group}",
        output_topic=f"{_RESULT_TOPIC}.{group}",
        payload_bytes=event.model_dump_json().encode("utf-8"),
        group_id=group,
    )
    assert len(history) == 1, "expected exactly one reduced-state event"
    result: dict[str, Any] = json.loads(history[0].value)
    return result


# ---------------------------------------------------------------------------
# Declared FSM state coverage — every outcome label entered, asserted literally.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestMergeSweepDeclaredStateCoverage:
    @pytest.mark.parametrize(
        ("outcome", "label"),
        [
            (EnumSweepOutcome.ARMED, "armed"),
            (EnumSweepOutcome.REBASED, "rebased"),
            (EnumSweepOutcome.CI_RERUN_TRIGGERED, "ci_rerun_triggered"),
        ],
    )
    async def test_transitional_outcome_state_entered(
        self,
        integration_event_bus: Any,
        outcome: EnumSweepOutcome,
        label: str,
    ) -> None:
        """Transitional states (armed/rebased/ci_rerun_triggered) fold non-terminal."""
        run_id, cid = uuid4(), uuid4()
        # total_prs=2 keeps the sweep non-terminal (only one PR classified).
        result = await _drive(
            _classified(101, outcome, total_prs=2, run_id=run_id, correlation_id=cid),
            integration_event_bus,
            group=f"merge-sweep-{label}",
        )
        state = result["state"]
        assert _outcome_label(state, f"{_REPO}#101") == label
        assert state["terminal_emitted"] is False

    async def test_pending_state_for_unclassified_pr(
        self, integration_event_bus: Any
    ) -> None:
        """`pending`: a PR in a multi-PR sweep with no classified outcome yet."""
        run_id, cid = uuid4(), uuid4()
        result = await _drive(
            _classified(
                101,
                EnumSweepOutcome.ARMED,
                total_prs=2,
                run_id=run_id,
                correlation_id=cid,
            ),
            integration_event_bus,
            group="merge-sweep-pending",
        )
        state = result["state"]
        # PR 101 was classified; PR 202 was never classified -> still pending.
        assert _outcome_label(state, f"{_REPO}#101") == "armed"
        assert _outcome_label(state, f"{_REPO}#202") == "pending"

    @pytest.mark.parametrize(
        ("outcome", "label"),
        [
            (EnumSweepOutcome.MERGED, "merged"),
            (EnumSweepOutcome.FAILED, "failed"),
            (EnumSweepOutcome.STUCK, "stuck"),
        ],
    )
    async def test_terminal_outcome_state_entered_and_emits_completed(
        self,
        integration_event_bus: Any,
        outcome: EnumSweepOutcome,
        label: str,
    ) -> None:
        """Terminal states (merged/failed/stuck): last PR of the sweep fires the
        exactly-once ``merge-sweep-completed.v1`` terminal intent."""
        run_id, cid = uuid4(), uuid4()
        # total_prs=1 -> this single classified PR completes the sweep.
        result = await _drive(
            _classified(303, outcome, total_prs=1, run_id=run_id, correlation_id=cid),
            integration_event_bus,
            group=f"merge-sweep-{label}",
        )
        state = result["state"]
        assert _outcome_label(state, f"{_REPO}#303") == label
        assert state["terminal_emitted"] is True
        # Exactly-once terminal bus-publish intent for the completed sweep.
        bus_intents = [
            i
            for i in result["intents"]
            if isinstance(i, dict) and i.get("topic") == TOPIC_SWEEP_COMPLETED
        ]
        assert len(bus_intents) == 1


# ---------------------------------------------------------------------------
# REDUCER idempotency + Phase 2 folds (pure delta — accumulation across events).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestMergeSweepReducerFolds:
    async def test_duplicate_outcome_is_first_writer_wins(self) -> None:
        """A duplicate classified outcome for a PR is a no-op (idempotent dedup)."""
        run_id, cid = uuid4(), uuid4()
        handler = HandlerMergeSweepStateReducer()
        state = ModelMergeSweepState(run_id=run_id, total_prs=3)
        event = _classified(
            404, EnumSweepOutcome.ARMED, total_prs=3, run_id=run_id, correlation_id=cid
        )

        state_after_first, _intents_first = handler.delta(state, event)
        assert state_after_first.armed_count == 1
        assert f"{_REPO}#404" in state_after_first.pr_outcomes_by_key

        # Fold the SAME PR outcome again -> no state change, no new intents.
        state_after_dup, intents_dup = handler.delta(state_after_first, event)
        assert state_after_dup.armed_count == 1  # counter unchanged
        assert intents_dup == []  # dedup short-circuits before any intent

    async def test_out_of_order_second_outcome_deduped(self) -> None:
        """A second (out-of-order) outcome for an already-recorded PR is ignored."""
        run_id, cid = uuid4(), uuid4()
        handler = HandlerMergeSweepStateReducer()
        state = ModelMergeSweepState(run_id=run_id, total_prs=2)

        armed = _classified(
            505, EnumSweepOutcome.ARMED, total_prs=2, run_id=run_id, correlation_id=cid
        )
        merged = _classified(
            505, EnumSweepOutcome.MERGED, total_prs=2, run_id=run_id, correlation_id=cid
        )
        state, _ = handler.delta(state, armed)
        state, _ = handler.delta(state, merged)  # arrives after armed -> ignored
        record = state.pr_outcomes_by_key[f"{_REPO}#505"]
        assert record.outcome == EnumSweepOutcome.ARMED  # first writer wins
        assert state.merged_count == 0

    async def test_phase2_thread_reply_folds(self) -> None:
        """Phase 2 thread-reply completion folds into aggregate + per-PR history."""
        run_id, cid = uuid4(), uuid4()
        handler = HandlerMergeSweepStateReducer()
        state = ModelMergeSweepState(run_id=run_id, total_prs=1)

        posted = ModelThreadRepliedEvent(
            correlation_id=cid,
            pr_number=606,
            repo=_REPO,
            reply_posted=True,
            is_draft=False,
        )
        state, _ = handler.delta(state, posted)
        assert state.thread_replies_posted == 1

        failed = ModelThreadRepliedEvent(
            correlation_id=cid,
            pr_number=606,
            repo=_REPO,
            reply_posted=False,
            is_draft=False,
        )
        state, _ = handler.delta(state, failed)
        assert state.thread_reply_failures == 1
        phase2 = state.pr_phase2_by_key[f"{_REPO}#606"]
        assert phase2.consecutive_failures == 1
        assert "thread_reply_failed" in phase2.last_failure_categories

    async def test_phase2_conflict_and_ci_fix_folds(self) -> None:
        """Phase 2 conflict-resolved + ci-fix folds, including the neutral no-op."""
        run_id, cid = uuid4(), uuid4()
        handler = HandlerMergeSweepStateReducer()
        state = ModelMergeSweepState(run_id=run_id, total_prs=1)

        resolved = ModelConflictResolvedEvent(
            correlation_id=cid,
            pr_number=707,
            repo=_REPO,
            resolution_committed=True,
        )
        state, _ = handler.delta(state, resolved)
        assert state.conflicts_resolved == 1

        # is_noop -> neutral: no counter movement, no failure recorded.
        noop = ModelConflictResolvedEvent(
            correlation_id=cid,
            pr_number=707,
            repo=_REPO,
            resolution_committed=False,
            is_noop=True,
        )
        state, _ = handler.delta(state, noop)
        assert state.conflict_hunk_failures == 0

        ci_fixed = CiFixResult(
            pr_number=707,
            repo=_REPO,
            run_id_github="gh-1",
            failing_job_name="lint",
            correlation_id=cid,
            patch_applied=True,
            local_tests_passed=True,
            is_noop=False,
        )
        state, _ = handler.delta(state, ci_fixed)
        assert state.ci_fixes_attempted == 1

        ci_failed = CiFixResult(
            pr_number=707,
            repo=_REPO,
            run_id_github="gh-2",
            failing_job_name="lint",
            correlation_id=cid,
            patch_applied=False,
            local_tests_passed=False,
            is_noop=False,
        )
        state, _ = handler.delta(state, ci_failed)
        assert state.ci_fix_failures == 1
