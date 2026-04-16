# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_merge_sweep_state_reducer [OMN-8964].

REDUCER node. Pure delta(state, event) -> (new_state, intents[]).
Returns dict {"state": ..., "intents": ...} per OMN-8950 convention.
RuntimeLocal (OMN-8946) constructs ModelStateEnvelope and persists via ProtocolStateStore.

Delta rules (authoritative):
1. Compute dedup key: f"{event.repo}#{event.pr_number}"
2. First-write-wins: if key in state.pr_outcomes_by_key → no state change, no intent.
3. First-write: add record, increment counter.
4. Terminal guard: if tracked == total and not terminal_emitted → flip flag, emit terminal.
5. Already terminal: no duplicate terminal even if more events arrive (step 2 short-circuits).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from omnimarket.nodes.node_merge_sweep_state_reducer.models.model_merge_sweep_state import (
    ModelMergeSweepState,
    ModelPrOutcomeRecord,
    is_terminal_outcome,
)
from omnimarket.nodes.node_sweep_outcome_classify.models.model_sweep_outcome import (
    EnumSweepOutcome,
    ModelSweepOutcomeClassified,
)

_log = logging.getLogger(__name__)

# Topics from contract.yaml
TOPIC_STATE_REDUCED = "onex.evt.omnimarket.merge-sweep-state-reduced.v1"
TOPIC_SWEEP_COMPLETED = "onex.evt.omnimarket.merge-sweep-completed.v1"


class HandlerMergeSweepStateReducer:
    """REDUCER: aggregate sweep state with first-writer-wins dedup + exactly-once terminal."""

    def delta(
        self,
        state: ModelMergeSweepState,
        event: ModelSweepOutcomeClassified,
    ) -> tuple[ModelMergeSweepState, list[dict[str, Any]]]:
        """Pure FSM delta. No I/O, no env reads, no bus publishes.

        Returns (new_state, intents). Intents are dicts with topic + payload
        for the adapter to publish.
        """
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
            first_seen_at=datetime.utcnow(),
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

        intents: list[dict[str, Any]] = []

        # Step 4: Terminal guard — emit exactly once when all PRs accounted for
        tracked_count = len(new_outcomes)
        if tracked_count == new_state.total_prs and not new_state.terminal_emitted:
            now = datetime.utcnow()
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

        return new_state, intents

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
            "intents": intents,
        }
