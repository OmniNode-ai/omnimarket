# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared-state coverage for node_pr_lifecycle_state_reducer (OMN-13674).

REDUCER archetype -> Variant B: the pure ``delta`` reducer is registered on the
canonical in-memory bus (``EventBusInmemory`` via the ``integration_event_bus``
fixture) through ``LocalRuntimeBusAdapter`` (``drive_round_trip``). A single
phase-advance command is published on the sweep-start topic and the reduced
state projection republished on the state-reduced topic is asserted.

This suite closes the ``state_machine`` declared-state set from ``contract.yaml``
(``idle -> inventorying -> triaged -> {fixing|merging} -> complete`` plus the
``... -> failed`` error edge) by asserting the *literal* lowercase state name
each fold lands in, and folds every declared subscribe-topic event type:

  * the FSM phase-advance events (start/inventory/triage/fix/merge/error), and
  * the additive repo-health lane folds (``repo-health-classified.v1`` and
    ``repo-health-repair-emitted.v1``, OMN-13585).

REDUCER idempotency dimensions covered: out-of-order rejection (source_phase
mismatch), correlation-id mismatch rejection, and repair-task-ref dedup on a
duplicate fold. No live Kafka / .201 — fully in-process.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
    HandlerPrLifecycleStateReducer,
)
from tests.integration._wave7_bus import drive_round_trip

_START_TOPIC = "onex.cmd.omnimarket.pr-lifecycle-sweep-start.v1"
_RESULT_TOPIC = "onex.evt.omnimarket.pr-lifecycle-state-reduced.v1"

_CLASSIFIED_TOPIC = "onex.evt.omnimarket.repo-health-classified.v1"
_REPAIR_TOPIC = "onex.evt.omnimarket.repo-health-repair-emitted.v1"


class _PrLifecycleBusHandler:
    """Bus-facing shim: forwards the decoded ``handle_dict`` payload to the reducer.

    ``LocalRuntimeBusAdapter`` fans the decoded dict in as keyword args (the
    handler declares ``**payload``); the reducer's ``handle_dict`` returns the
    ``{"state": ..., "intents": ...}`` convention dict which the adapter
    republishes to the output topic.
    """

    def __init__(self) -> None:
        self._handler = HandlerPrLifecycleStateReducer()

    async def handle(self, **payload: Any) -> dict[str, Any]:
        return self._handler.handle_dict(payload)


async def _drive(bus: Any, payload: dict[str, Any], *, group: str) -> dict[str, Any]:
    """Publish one reducer command over the bus and return the reduced result dict.

    The republish output topic is suffixed with ``group`` so a test that drives
    more than one round-trip on the shared ``integration_event_bus`` isolates
    each drive's ``pr-lifecycle-state-reduced.v1`` history (in-memory bus history
    is cumulative per topic), preserving the exactly-once republish assertion.
    """
    output_topic = f"{_RESULT_TOPIC}.{group}"
    history = await drive_round_trip(
        bus,
        handler=_PrLifecycleBusHandler(),
        handler_name="pr-lifecycle-state-reducer",
        input_model_cls=None,
        start_topic=f"{_START_TOPIC}.{group}",
        output_topic=output_topic,
        payload_bytes=json.dumps(payload).encode("utf-8"),
        group_id=group,
    )
    assert len(history) == 1, "expected exactly one reduced-state event"
    result: dict[str, Any] = json.loads(history[0].value)
    return result


def _fsm_payload(
    *,
    state_phase: str,
    source_phase: str,
    trigger: str,
    correlation_id: str,
    state_correlation_id: str | None = None,
    success: bool = True,
    error_message: str | None = None,
    entry_flags: dict[str, bool] | None = None,
    **event_extra: Any,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "correlation_id": state_correlation_id or correlation_id,
        "phase": state_phase,
    }
    if entry_flags is not None:
        state["entry_flags"] = entry_flags
    return {
        "state": state,
        "event": {
            "correlation_id": correlation_id,
            "source_phase": source_phase,
            "trigger": trigger,
            "success": success,
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "error_message": error_message,
            **event_extra,
        },
    }


# ---------------------------------------------------------------------------
# Declared FSM state coverage — every state entered, asserted by literal name.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestPrLifecycleDeclaredStateCoverage:
    async def test_idle_state_holds_on_out_of_order_event(
        self, integration_event_bus: Any
    ) -> None:
        """`idle`: an out-of-order event (source_phase != current) is rejected."""
        cid = str(uuid4())
        # State is idle but the event claims to come from the fixing phase.
        result = await _drive(
            integration_event_bus,
            _fsm_payload(
                state_phase="idle",
                source_phase="fixing",
                trigger="fixes_complete",
                correlation_id=cid,
            ),
            group="pr-lifecycle-idle",
        )
        assert result["state"]["phase"] == "idle"
        assert result["intents"] == []

    async def test_inventorying_state_entered(self, integration_event_bus: Any) -> None:
        """`inventorying`: idle + start_received -> inventorying + start_inventory."""
        cid = str(uuid4())
        result = await _drive(
            integration_event_bus,
            _fsm_payload(
                state_phase="idle",
                source_phase="idle",
                trigger="start_received",
                correlation_id=cid,
            ),
            group="pr-lifecycle-inv",
        )
        assert result["state"]["phase"] == "inventorying"
        assert result["state"]["started_at"] is not None
        assert len(result["intents"]) == 1
        assert result["intents"][0]["intent_type"] == "pr_lifecycle.start_inventory"

    async def test_triaged_state_entered(self, integration_event_bus: Any) -> None:
        """`triaged`: inventorying + inventory_complete -> triaged (no intent)."""
        cid = str(uuid4())
        result = await _drive(
            integration_event_bus,
            _fsm_payload(
                state_phase="inventorying",
                source_phase="inventorying",
                trigger="inventory_complete",
                correlation_id=cid,
                prs_inventoried=7,
            ),
            group="pr-lifecycle-triaged",
        )
        assert result["state"]["phase"] == "triaged"
        assert result["state"]["prs_inventoried"] == 7
        assert result["intents"] == []

    async def test_fixing_state_entered(self, integration_event_bus: Any) -> None:
        """`fixing`: triaged + fixes_pending -> fixing + start_fix."""
        cid = str(uuid4())
        result = await _drive(
            integration_event_bus,
            _fsm_payload(
                state_phase="triaged",
                source_phase="triaged",
                trigger="fixes_pending",
                correlation_id=cid,
                prs_blocked=3,
            ),
            group="pr-lifecycle-fixing",
        )
        assert result["state"]["phase"] == "fixing"
        assert result["state"]["prs_blocked"] == 3
        assert result["intents"][0]["intent_type"] == "pr_lifecycle.start_fix"

    async def test_merging_state_entered(self, integration_event_bus: Any) -> None:
        """`merging`: fixing + fixes_complete -> merging + start_merge."""
        cid = str(uuid4())
        result = await _drive(
            integration_event_bus,
            _fsm_payload(
                state_phase="fixing",
                source_phase="fixing",
                trigger="fixes_complete",
                correlation_id=cid,
                prs_fixed=2,
            ),
            group="pr-lifecycle-merging",
        )
        assert result["state"]["phase"] == "merging"
        assert result["state"]["prs_fixed"] == 2
        assert result["intents"][0]["intent_type"] == "pr_lifecycle.start_merge"

    async def test_merging_via_no_fixes_needed(
        self, integration_event_bus: Any
    ) -> None:
        """`merging` alt edge: triaged + no_fixes_needed -> merging."""
        cid = str(uuid4())
        result = await _drive(
            integration_event_bus,
            _fsm_payload(
                state_phase="triaged",
                source_phase="triaged",
                trigger="no_fixes_needed",
                correlation_id=cid,
            ),
            group="pr-lifecycle-merging-alt",
        )
        assert result["state"]["phase"] == "merging"
        assert result["intents"][0]["intent_type"] == "pr_lifecycle.start_merge"

    async def test_complete_terminal_state_entered(
        self, integration_event_bus: Any
    ) -> None:
        """`complete`: merging + merge_complete -> complete + sweep_complete."""
        cid = str(uuid4())
        result = await _drive(
            integration_event_bus,
            _fsm_payload(
                state_phase="merging",
                source_phase="merging",
                trigger="merge_complete",
                correlation_id=cid,
                prs_merged=5,
            ),
            group="pr-lifecycle-complete",
        )
        assert result["state"]["phase"] == "complete"
        assert result["state"]["prs_merged"] == 5
        assert result["intents"][0]["intent_type"] == "pr_lifecycle.sweep_complete"

    async def test_failed_terminal_state_entered(
        self, integration_event_bus: Any
    ) -> None:
        """`failed`: inventorying + error -> failed + sweep_failed, error captured."""
        cid = str(uuid4())
        result = await _drive(
            integration_event_bus,
            _fsm_payload(
                state_phase="inventorying",
                source_phase="inventorying",
                trigger="error",
                correlation_id=cid,
                success=False,
                error_message="gh CLI exploded",
            ),
            group="pr-lifecycle-failed",
        )
        assert result["state"]["phase"] == "failed"
        assert result["state"]["error_message"] == "gh CLI exploded"
        assert result["intents"][0]["intent_type"] == "pr_lifecycle.sweep_failed"


# ---------------------------------------------------------------------------
# Fold every declared event type — additive repo-health lane (OMN-13585).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestPrLifecycleRepoHealthFolds:
    @pytest.mark.parametrize(
        ("classification", "counter_field"),
        [
            ("pr_scoped", "pr_scoped_count"),
            ("repo_baseline", "repo_baseline_count"),
            ("external_dependency", "external_dependency_count"),
            ("unknown", "unknown_count"),
            ("garbage-label", "unknown_count"),  # unknown labels bucket to unknown
        ],
    )
    async def test_repo_health_classified_folds_into_subrecord(
        self,
        integration_event_bus: Any,
        classification: str,
        counter_field: str,
    ) -> None:
        """`repo-health-classified.v1` folds ONLY into the repo_health sub-record."""
        cid = str(uuid4())
        payload = {
            "state": {"correlation_id": cid, "phase": "inventorying"},
            "event": {"classification": classification, "ticket_ref": "OMN-1"},
            "event_topic": _CLASSIFIED_TOPIC,
        }
        result = await _drive(
            integration_event_bus, payload, group=f"rh-classified-{classification}"
        )
        rh = result["state"]["repo_health"]
        assert rh["classified_count"] == 1
        assert rh[counter_field] == 1
        # PR-lane FSM fields are untouched by the additive fold.
        assert result["state"]["phase"] == "inventorying"
        assert result["intents"] == []

    async def test_repo_health_repair_emitted_folds_and_dedups(
        self, integration_event_bus: Any
    ) -> None:
        """`repo-health-repair-emitted.v1` fold; duplicate ticket_ref is deduped."""
        cid = str(uuid4())
        first = await _drive(
            integration_event_bus,
            {
                "state": {"correlation_id": cid, "phase": "triaged"},
                "event": {"ticket_ref": "OMN-42"},
                "event_topic": _REPAIR_TOPIC,
            },
            group="rh-repair-1",
        )
        rh1 = first["state"]["repo_health"]
        assert rh1["repair_tasks_emitted"] == 1
        assert rh1["repair_task_refs"] == ["OMN-42"]

        # Fold a SECOND repair for the same ticket over the bus, feeding the
        # prior projection back in: emitted count rises, refs stay deduped.
        second = await _drive(
            integration_event_bus,
            {
                "state": first["state"],
                "event": {"ticket_ref": "OMN-42"},
                "event_topic": _REPAIR_TOPIC,
            },
            group="rh-repair-2",
        )
        rh2 = second["state"]["repo_health"]
        assert rh2["repair_tasks_emitted"] == 2
        assert rh2["repair_task_refs"] == ["OMN-42"]  # deduped, not duplicated


# ---------------------------------------------------------------------------
# Reject / idempotency dimensions.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestPrLifecycleRejections:
    async def test_correlation_id_mismatch_rejected(
        self, integration_event_bus: Any
    ) -> None:
        """An event whose correlation_id differs from the state is rejected."""
        result = await _drive(
            integration_event_bus,
            _fsm_payload(
                state_phase="idle",
                source_phase="idle",
                trigger="start_received",
                correlation_id=str(uuid4()),
                state_correlation_id=str(uuid4()),  # different id
            ),
            group="pr-lifecycle-corr-mismatch",
        )
        assert result["state"]["phase"] == "idle"
        assert result["intents"] == []

    async def test_terminal_complete_rejects_further_events(
        self, integration_event_bus: Any
    ) -> None:
        """A terminal `complete` state rejects any subsequent event."""
        cid = str(uuid4())
        result = await _drive(
            integration_event_bus,
            _fsm_payload(
                state_phase="complete",
                source_phase="complete",
                trigger="start_received",
                correlation_id=cid,
            ),
            group="pr-lifecycle-terminal-complete",
        )
        assert result["state"]["phase"] == "complete"
        assert result["intents"] == []

    async def test_dry_run_suppresses_side_effect_intent(
        self, integration_event_bus: Any
    ) -> None:
        """`dry_run` entry flag suppresses the start_inventory side-effect intent."""
        cid = str(uuid4())
        result = await _drive(
            integration_event_bus,
            _fsm_payload(
                state_phase="idle",
                source_phase="idle",
                trigger="start_received",
                correlation_id=cid,
                entry_flags={"dry_run": True},
            ),
            group="pr-lifecycle-dry-run",
        )
        # Transition still occurs, but the side-effect intent is suppressed.
        assert result["state"]["phase"] == "inventorying"
        assert result["intents"] == []
