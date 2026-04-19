# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_merge_sweep_state_reducer [OMN-8964, OMN-8997].

REDUCER node. Pure delta(state, event) -> (new_state, intents[]).
Returns dict {"state": ..., "intents": ...} per OMN-8950 convention.

Intents are a heterogeneous list:
    - ``ModelPersistStateIntent`` — appended on every first-write mutation
      (OMN-9010 / pure-reducer epic OMN-9006). Consumed by
      ``node_state_persist_effect`` which writes to ``ProtocolStateStore``.
    - ``dict[str, Any]`` with ``{topic, payload}`` — existing bus-publish
      intent for the terminal ``merge-sweep-completed.v1`` event.

Delta rules — Phase 1 (authoritative):
1. Compute dedup key: f"{event.repo}#{event.pr_number}"
2. First-write-wins: if key in state.pr_outcomes_by_key -> no state change, no intent.
3. First-write: add record, increment counter, append persist intent.
4. Terminal guard: if tracked == total and not terminal_emitted -> flip flag,
   append bus-publish terminal intent; persist intent is appended last so it
   carries the updated (terminal-emitted) state.
5. Already terminal: no duplicate terminal even if more events arrive (step 2
   short-circuits).

Delta rules — Phase 2 [OMN-8997] (authoritative):
P1. ModelThreadRepliedEvent:
    - reply_posted=True  → thread_replies_posted +1; reset consecutive_failures to 0
    - reply_posted=False → thread_reply_failures +1; consecutive_failures +1;
      append "thread_reply_failed" to last_failure_categories
P2. ModelConflictResolvedEvent:
    - resolution_committed=True          → conflicts_resolved +1; reset failures
    - resolution_committed=False AND NOT is_noop → conflict_hunk_failures +1; failures +1;
      append "conflict_hunk_failed"
    - is_noop=True                       → no failure change (neutral)
P3. CiFixResult:
    - patch_applied=True   → ci_fixes_attempted +1; reset failures
    - patch_applied=False AND NOT is_noop → ci_fix_failures +1; failures +1;
      append "ci_fix_failed"
    - is_noop=True         → no failure change (neutral)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from omnibase_core.models.intents import ModelPersistStateIntent
from omnibase_core.models.state.model_state_envelope import ModelStateEnvelope

from omnimarket.nodes.node_ci_fix_effect.models.model_ci_fix_result import CiFixResult
from omnimarket.nodes.node_merge_sweep_state_reducer.models.model_merge_sweep_state import (
    ModelMergeSweepState,
    ModelPrOutcomeRecord,
    ModelPrPhase2Record,
    is_terminal_outcome,
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

_log = logging.getLogger(__name__)

# Topics from contract.yaml
TOPIC_STATE_REDUCED = "onex.evt.omnimarket.merge-sweep-state-reduced.v1"
TOPIC_SWEEP_COMPLETED = "onex.evt.omnimarket.merge-sweep-completed.v1"

NODE_ID = "node_merge_sweep_state_reducer"

Phase2Event = ModelThreadRepliedEvent | ModelConflictResolvedEvent | CiFixResult
AnyReducerEvent = ModelSweepOutcomeClassified | Phase2Event


def _build_persist_intent(
    state: ModelMergeSweepState, correlation_id: Any
) -> ModelPersistStateIntent:
    now = datetime.now(UTC)
    envelope = ModelStateEnvelope(
        node_id=NODE_ID,
        scope_id=str(state.run_id),
        data=state.model_dump(mode="json"),
        written_at=now,
    )
    return ModelPersistStateIntent(
        intent_id=uuid4(),
        envelope=envelope,
        emitted_at=now,
        correlation_id=correlation_id,
    )


def _get_or_create_phase2_record(
    state: ModelMergeSweepState, key: str, pr_number: int, repo: str
) -> ModelPrPhase2Record:
    return state.pr_phase2_by_key.get(
        key, ModelPrPhase2Record(pr_number=pr_number, repo=repo)
    )


class HandlerMergeSweepStateReducer:
    """REDUCER: aggregate sweep state with first-writer-wins dedup + exactly-once terminal."""

    def delta(
        self,
        state: ModelMergeSweepState,
        event: AnyReducerEvent,
    ) -> tuple[ModelMergeSweepState, list[ModelPersistStateIntent | dict[str, Any]]]:
        """Pure FSM delta. No I/O, no env reads, no bus publishes.

        Dispatches to _delta_phase1 for ModelSweepOutcomeClassified events,
        or _delta_phase2 for Phase 2 completion events.
        """
        if isinstance(event, ModelSweepOutcomeClassified):
            return self._delta_phase1(state, event)
        return self._delta_phase2(state, event)

    def _delta_phase1(
        self,
        state: ModelMergeSweepState,
        event: ModelSweepOutcomeClassified,
    ) -> tuple[ModelMergeSweepState, list[ModelPersistStateIntent | dict[str, Any]]]:
        dedup_key = f"{event.repo}#{event.pr_number}"

        # Step 2: First-write-wins dedup
        if dedup_key in state.pr_outcomes_by_key:
            _log.debug(
                "Reducer dedup: %s already recorded for run %s — skipping",
                dedup_key,
                state.run_id,
            )
            return state, []

        # Step 3: First-write — build record and increment counter
        record = ModelPrOutcomeRecord(
            pr_number=event.pr_number,
            repo=event.repo,
            outcome=event.outcome,
            terminal=is_terminal_outcome(event.outcome),
            first_seen_at=datetime.now(UTC),
            classified_event_id=event.event_id,
        )

        new_outcomes = {**state.pr_outcomes_by_key, dedup_key: record}

        # Increment the correct counter
        counter_updates: dict[str, int] = {}
        if event.outcome == EnumSweepOutcome.MERGED:
            counter_updates["merged_count"] = state.merged_count + 1
        elif event.outcome == EnumSweepOutcome.ARMED:
            counter_updates["armed_count"] = state.armed_count + 1
        elif event.outcome == EnumSweepOutcome.REBASED:
            counter_updates["rebased_count"] = state.rebased_count + 1
        elif event.outcome == EnumSweepOutcome.CI_RERUN_TRIGGERED:
            counter_updates["ci_rerun_count"] = state.ci_rerun_count + 1
        elif event.outcome == EnumSweepOutcome.FAILED:
            counter_updates["failed_count"] = state.failed_count + 1
        elif event.outcome == EnumSweepOutcome.STUCK:
            counter_updates["stuck_count"] = state.stuck_count + 1

        new_state = state.model_copy(
            update={
                "pr_outcomes_by_key": new_outcomes,
                **counter_updates,
            }
        )

        intents: list[ModelPersistStateIntent | dict[str, Any]] = []

        # Step 4: Terminal guard — emit exactly once when all PRs accounted for
        tracked_count = len(new_outcomes)
        if tracked_count == new_state.total_prs and not new_state.terminal_emitted:
            now = datetime.now(UTC)
            new_state = new_state.model_copy(
                update={
                    "terminal_emitted": True,
                    "completed_at": now,
                }
            )
            intents.append(
                {
                    "topic": TOPIC_SWEEP_COMPLETED,
                    "payload": {
                        "run_id": str(new_state.run_id),
                        "total_prs": new_state.total_prs,
                        "merged_count": new_state.merged_count,
                        "armed_count": new_state.armed_count,
                        "rebased_count": new_state.rebased_count,
                        "ci_rerun_count": new_state.ci_rerun_count,
                        "failed_count": new_state.failed_count,
                        "stuck_count": new_state.stuck_count,
                        "completed_at": now.isoformat(),
                    },
                }
            )
            _log.info(
                "Reducer terminal: run %s complete (%d/%d PRs, %d armed, %d rebased, "
                "%d ci_rerun, %d failed, %d stuck)",
                new_state.run_id,
                tracked_count,
                new_state.total_prs,
                new_state.armed_count,
                new_state.rebased_count,
                new_state.ci_rerun_count,
                new_state.failed_count,
                new_state.stuck_count,
            )

        # Append persist intent last so the existing bus-publish indexing at
        # ``intents[0]`` on terminal remains stable for downstream consumers.
        intents.append(_build_persist_intent(new_state, event.correlation_id))

        return new_state, intents

    def _delta_phase2(
        self,
        state: ModelMergeSweepState,
        event: Phase2Event,
    ) -> tuple[ModelMergeSweepState, list[ModelPersistStateIntent | dict[str, Any]]]:
        """Apply a Phase 2 polish-task completion event to aggregate state."""
        key = f"{event.repo}#{event.pr_number}"
        phase2_rec = _get_or_create_phase2_record(
            state, key, event.pr_number, event.repo
        )

        state_updates: dict[str, Any] = {}

        if isinstance(event, ModelThreadRepliedEvent):
            if event.reply_posted:
                state_updates["thread_replies_posted"] = state.thread_replies_posted + 1
                phase2_rec = phase2_rec.model_copy(update={"consecutive_failures": 0})
            else:
                state_updates["thread_reply_failures"] = state.thread_reply_failures + 1
                phase2_rec = phase2_rec.model_copy(
                    update={
                        "consecutive_failures": phase2_rec.consecutive_failures + 1,
                        "last_failure_categories": [
                            *phase2_rec.last_failure_categories,
                            "thread_reply_failed",
                        ],
                    }
                )

        elif isinstance(event, ModelConflictResolvedEvent):
            if event.resolution_committed:
                state_updates["conflicts_resolved"] = state.conflicts_resolved + 1
                phase2_rec = phase2_rec.model_copy(update={"consecutive_failures": 0})
            elif not event.is_noop:
                state_updates["conflict_hunk_failures"] = (
                    state.conflict_hunk_failures + 1
                )
                phase2_rec = phase2_rec.model_copy(
                    update={
                        "consecutive_failures": phase2_rec.consecutive_failures + 1,
                        "last_failure_categories": [
                            *phase2_rec.last_failure_categories,
                            "conflict_hunk_failed",
                        ],
                    }
                )
            # is_noop=True: neutral — no update to phase2_rec or state

        elif isinstance(event, CiFixResult):
            if event.patch_applied:
                state_updates["ci_fixes_attempted"] = state.ci_fixes_attempted + 1
                phase2_rec = phase2_rec.model_copy(update={"consecutive_failures": 0})
            elif not event.is_noop:
                state_updates["ci_fix_failures"] = state.ci_fix_failures + 1
                phase2_rec = phase2_rec.model_copy(
                    update={
                        "consecutive_failures": phase2_rec.consecutive_failures + 1,
                        "last_failure_categories": [
                            *phase2_rec.last_failure_categories,
                            "ci_fix_failed",
                        ],
                    }
                )
            # is_noop=True: neutral — no update to phase2_rec or state

        new_phase2 = {**state.pr_phase2_by_key, key: phase2_rec}
        new_state = state.model_copy(
            update={
                "pr_phase2_by_key": new_phase2,
                **state_updates,
            }
        )

        return new_state, [_build_persist_intent(new_state, event.correlation_id)]

    def handle(self, request: ModelSweepOutcomeClassified) -> dict[str, Any]:
        """RuntimeLocal-protocol shim. Wraps delta() in dict convention.

        Seeds from ModelMergeSweepState with defaults on first invocation.
        Subsequent invocations would read from ProtocolStateStore (OMN-8946 wiring).
        For Phase 1 proof: seeds from first event's run_id + total_prs.
        """
        initial = ModelMergeSweepState(
            run_id=request.run_id,
            total_prs=request.total_prs,
        )
        new_state, intents = self.delta(initial, request)
        return {
            "state": new_state.model_dump(mode="json"),
            "intents": [
                (
                    i.model_dump(mode="json")
                    if isinstance(i, ModelPersistStateIntent)
                    else i
                )
                for i in intents
            ],
        }
