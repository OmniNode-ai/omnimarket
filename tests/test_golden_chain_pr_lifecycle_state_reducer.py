# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_pr_lifecycle_state_reducer.

Covers all FSM state transitions, entry flag combinations (dry_run,
inventory_only, fix_only), terminal state rejection, correlation ID
mismatch rejection, out-of-order event rejection, and EventBusInmemory
wiring. [OMN-8086]
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
    HandlerPrLifecycleStateReducer,
)
from omnimarket.nodes.node_pr_lifecycle_state_reducer.models.model_pr_lifecycle_event import (
    EnumPrLifecycleEventTrigger,
    EnumPrLifecyclePhase,
    ModelPrLifecycleEvent,
)
from omnimarket.nodes.node_pr_lifecycle_state_reducer.models.model_pr_lifecycle_intent import (
    EnumPrLifecycleIntentType,
)
from omnimarket.nodes.node_pr_lifecycle_state_reducer.models.model_pr_lifecycle_state import (
    ModelPrLifecycleEntryFlags,
    ModelPrLifecycleState,
)

CMD_TOPIC = "onex.cmd.omnimarket.pr-lifecycle-sweep-start.v1"
RESULT_TOPIC = "onex.evt.omnimarket.pr-lifecycle-state-reduced.v1"


def _state(
    phase: EnumPrLifecyclePhase = EnumPrLifecyclePhase.IDLE,
    **kwargs: object,
) -> ModelPrLifecycleState:
    cid = kwargs.pop("correlation_id", uuid4())  # type: ignore[arg-type]
    return ModelPrLifecycleState(correlation_id=cid, phase=phase, **kwargs)  # type: ignore[arg-type]


def _event(
    source_phase: EnumPrLifecyclePhase,
    trigger: EnumPrLifecycleEventTrigger,
    correlation_id: object,
    success: bool = True,
    error_message: str | None = None,
    **kwargs: object,
) -> ModelPrLifecycleEvent:
    return ModelPrLifecycleEvent(
        correlation_id=correlation_id,  # type: ignore[arg-type]
        source_phase=source_phase,
        trigger=trigger,
        success=success,
        timestamp=datetime.now(tz=UTC),
        error_message=error_message,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.unit
class TestPrLifecycleStateReducerGoldenChain:
    """Golden chain: (state, event) -> (new_state, intents[])."""

    # ------------------------------------------------------------------ #
    # Happy-path transitions                                               #
    # ------------------------------------------------------------------ #

    def test_idle_to_inventorying(self) -> None:
        """IDLE + START_RECEIVED -> INVENTORYING with START_INVENTORY intent."""
        handler = HandlerPrLifecycleStateReducer()
        state = _state()
        event = _event(
            EnumPrLifecyclePhase.IDLE,
            EnumPrLifecycleEventTrigger.START_RECEIVED,
            state.correlation_id,
        )

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.INVENTORYING
        assert new_state.started_at is not None
        assert len(intents) == 1
        assert intents[0].intent_type == EnumPrLifecycleIntentType.START_INVENTORY

    def test_inventorying_to_triaged(self) -> None:
        """INVENTORYING + INVENTORY_COMPLETE -> TRIAGED, no intent emitted."""
        handler = HandlerPrLifecycleStateReducer()
        cid = uuid4()
        state = _state(phase=EnumPrLifecyclePhase.INVENTORYING, correlation_id=cid)
        event = _event(
            EnumPrLifecyclePhase.INVENTORYING,
            EnumPrLifecycleEventTrigger.INVENTORY_COMPLETE,
            cid,
            prs_inventoried=10,
        )

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.TRIAGED
        assert new_state.prs_inventoried == 10
        assert len(intents) == 0

    def test_triaged_to_fixing_when_fixes_pending(self) -> None:
        """TRIAGED + FIXES_PENDING -> FIXING with START_FIX intent."""
        handler = HandlerPrLifecycleStateReducer()
        cid = uuid4()
        state = _state(phase=EnumPrLifecyclePhase.TRIAGED, correlation_id=cid)
        event = _event(
            EnumPrLifecyclePhase.TRIAGED,
            EnumPrLifecycleEventTrigger.FIXES_PENDING,
            cid,
            prs_blocked=3,
        )

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.FIXING
        assert new_state.prs_blocked == 3
        assert len(intents) == 1
        assert intents[0].intent_type == EnumPrLifecycleIntentType.START_FIX

    def test_triaged_to_merging_when_no_fixes_needed(self) -> None:
        """TRIAGED + NO_FIXES_NEEDED -> MERGING with START_MERGE intent."""
        handler = HandlerPrLifecycleStateReducer()
        cid = uuid4()
        state = _state(phase=EnumPrLifecyclePhase.TRIAGED, correlation_id=cid)
        event = _event(
            EnumPrLifecyclePhase.TRIAGED,
            EnumPrLifecycleEventTrigger.NO_FIXES_NEEDED,
            cid,
        )

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.MERGING
        assert len(intents) == 1
        assert intents[0].intent_type == EnumPrLifecycleIntentType.START_MERGE

    def test_fixing_to_merging(self) -> None:
        """FIXING + FIXES_COMPLETE -> MERGING with START_MERGE intent."""
        handler = HandlerPrLifecycleStateReducer()
        cid = uuid4()
        state = _state(phase=EnumPrLifecyclePhase.FIXING, correlation_id=cid)
        event = _event(
            EnumPrLifecyclePhase.FIXING,
            EnumPrLifecycleEventTrigger.FIXES_COMPLETE,
            cid,
            prs_fixed=2,
        )

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.MERGING
        assert new_state.prs_fixed == 2
        assert len(intents) == 1
        assert intents[0].intent_type == EnumPrLifecycleIntentType.START_MERGE

    def test_merging_to_complete(self) -> None:
        """MERGING + MERGE_COMPLETE -> COMPLETE with SWEEP_COMPLETE intent."""
        handler = HandlerPrLifecycleStateReducer()
        cid = uuid4()
        state = _state(
            phase=EnumPrLifecyclePhase.MERGING,
            correlation_id=cid,
            prs_inventoried=5,
        )
        event = _event(
            EnumPrLifecyclePhase.MERGING,
            EnumPrLifecycleEventTrigger.MERGE_COMPLETE,
            cid,
            prs_merged=5,
        )

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.COMPLETE
        assert new_state.prs_merged == 5
        assert new_state.prs_processed == 5
        assert len(intents) == 1
        assert intents[0].intent_type == EnumPrLifecycleIntentType.SWEEP_COMPLETE

    def test_full_happy_path_no_fixes(self) -> None:
        """Walk IDLE -> INVENTORYING -> TRIAGED -> MERGING -> COMPLETE (no fixes)."""
        handler = HandlerPrLifecycleStateReducer()
        state = _state()
        cid = state.correlation_id

        transitions = [
            (EnumPrLifecyclePhase.IDLE, EnumPrLifecycleEventTrigger.START_RECEIVED),
            (
                EnumPrLifecyclePhase.INVENTORYING,
                EnumPrLifecycleEventTrigger.INVENTORY_COMPLETE,
            ),
            (EnumPrLifecyclePhase.TRIAGED, EnumPrLifecycleEventTrigger.NO_FIXES_NEEDED),
            (EnumPrLifecyclePhase.MERGING, EnumPrLifecycleEventTrigger.MERGE_COMPLETE),
        ]
        expected_phases = [
            EnumPrLifecyclePhase.INVENTORYING,
            EnumPrLifecyclePhase.TRIAGED,
            EnumPrLifecyclePhase.MERGING,
            EnumPrLifecyclePhase.COMPLETE,
        ]

        for (from_phase, trigger), expected in zip(
            transitions, expected_phases, strict=False
        ):
            event = _event(from_phase, trigger, cid)
            state, _ = handler.delta(state, event)
            assert state.phase == expected

    def test_full_happy_path_with_fixes(self) -> None:
        """Walk IDLE -> ... -> FIXING -> MERGING -> COMPLETE (with fixes)."""
        handler = HandlerPrLifecycleStateReducer()
        state = _state()
        cid = state.correlation_id

        transitions = [
            (EnumPrLifecyclePhase.IDLE, EnumPrLifecycleEventTrigger.START_RECEIVED),
            (
                EnumPrLifecyclePhase.INVENTORYING,
                EnumPrLifecycleEventTrigger.INVENTORY_COMPLETE,
            ),
            (EnumPrLifecyclePhase.TRIAGED, EnumPrLifecycleEventTrigger.FIXES_PENDING),
            (EnumPrLifecyclePhase.FIXING, EnumPrLifecycleEventTrigger.FIXES_COMPLETE),
            (EnumPrLifecyclePhase.MERGING, EnumPrLifecycleEventTrigger.MERGE_COMPLETE),
        ]
        expected_phases = [
            EnumPrLifecyclePhase.INVENTORYING,
            EnumPrLifecyclePhase.TRIAGED,
            EnumPrLifecyclePhase.FIXING,
            EnumPrLifecyclePhase.MERGING,
            EnumPrLifecyclePhase.COMPLETE,
        ]

        for (from_phase, trigger), expected in zip(
            transitions, expected_phases, strict=False
        ):
            event = _event(from_phase, trigger, cid)
            state, _ = handler.delta(state, event)
            assert state.phase == expected

    # ------------------------------------------------------------------ #
    # Error transitions                                                    #
    # ------------------------------------------------------------------ #

    def test_inventorying_to_failed_on_error(self) -> None:
        """INVENTORYING + ERROR -> FAILED with SWEEP_FAILED intent."""
        handler = HandlerPrLifecycleStateReducer()
        cid = uuid4()
        state = _state(phase=EnumPrLifecyclePhase.INVENTORYING, correlation_id=cid)
        event = _event(
            EnumPrLifecyclePhase.INVENTORYING,
            EnumPrLifecycleEventTrigger.ERROR,
            cid,
            success=False,
            error_message="gh CLI failed",
        )

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.FAILED
        assert new_state.error_message == "gh CLI failed"
        assert len(intents) == 1
        assert intents[0].intent_type == EnumPrLifecycleIntentType.SWEEP_FAILED

    def test_triaged_to_failed_on_error(self) -> None:
        """TRIAGED + ERROR -> FAILED."""
        handler = HandlerPrLifecycleStateReducer()
        cid = uuid4()
        state = _state(phase=EnumPrLifecyclePhase.TRIAGED, correlation_id=cid)
        event = _event(
            EnumPrLifecyclePhase.TRIAGED,
            EnumPrLifecycleEventTrigger.ERROR,
            cid,
            success=False,
            error_message="triage error",
        )

        new_state, _ = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.FAILED
        assert new_state.error_message == "triage error"

    def test_fixing_to_failed_on_error(self) -> None:
        """FIXING + ERROR -> FAILED."""
        handler = HandlerPrLifecycleStateReducer()
        cid = uuid4()
        state = _state(phase=EnumPrLifecyclePhase.FIXING, correlation_id=cid)
        event = _event(
            EnumPrLifecyclePhase.FIXING,
            EnumPrLifecycleEventTrigger.ERROR,
            cid,
            success=False,
            error_message="fix dispatch failed",
        )

        new_state, _ = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.FAILED

    def test_merging_to_failed_on_error(self) -> None:
        """MERGING + ERROR -> FAILED."""
        handler = HandlerPrLifecycleStateReducer()
        cid = uuid4()
        state = _state(phase=EnumPrLifecyclePhase.MERGING, correlation_id=cid)
        event = _event(
            EnumPrLifecyclePhase.MERGING,
            EnumPrLifecycleEventTrigger.ERROR,
            cid,
            success=False,
            error_message="merge error",
        )

        new_state, _ = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.FAILED

    # ------------------------------------------------------------------ #
    # Rejection behavior                                                   #
    # ------------------------------------------------------------------ #

    def test_correlation_id_mismatch_rejected(self) -> None:
        """Event with wrong correlation_id is rejected — state unchanged."""
        handler = HandlerPrLifecycleStateReducer()
        state = _state()
        event = _event(
            EnumPrLifecyclePhase.IDLE,
            EnumPrLifecycleEventTrigger.START_RECEIVED,
            uuid4(),  # different correlation_id
        )

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.IDLE
        assert len(intents) == 0

    def test_out_of_order_event_rejected(self) -> None:
        """Event with wrong source_phase is rejected — state unchanged."""
        handler = HandlerPrLifecycleStateReducer()
        state = _state()
        event = _event(
            EnumPrLifecyclePhase.FIXING,  # wrong: state is IDLE
            EnumPrLifecycleEventTrigger.FIXES_COMPLETE,
            state.correlation_id,
        )

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.IDLE
        assert len(intents) == 0

    def test_terminal_complete_rejects_events(self) -> None:
        """COMPLETE state rejects all events."""
        handler = HandlerPrLifecycleStateReducer()
        cid = uuid4()
        state = _state(phase=EnumPrLifecyclePhase.COMPLETE, correlation_id=cid)
        event = _event(
            EnumPrLifecyclePhase.COMPLETE,
            EnumPrLifecycleEventTrigger.START_RECEIVED,
            cid,
        )

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.COMPLETE
        assert len(intents) == 0

    def test_terminal_failed_rejects_events(self) -> None:
        """FAILED state rejects all events."""
        handler = HandlerPrLifecycleStateReducer()
        cid = uuid4()
        state = _state(phase=EnumPrLifecyclePhase.FAILED, correlation_id=cid)
        event = _event(
            EnumPrLifecyclePhase.FAILED,
            EnumPrLifecycleEventTrigger.ERROR,
            cid,
        )

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.FAILED
        assert len(intents) == 0

    def test_undefined_transition_returns_unchanged_state(self) -> None:
        """A (state, trigger) pair with no defined transition returns state unchanged."""
        handler = HandlerPrLifecycleStateReducer()
        cid = uuid4()
        # IDLE does not have INVENTORY_COMPLETE trigger
        state = _state(phase=EnumPrLifecyclePhase.IDLE, correlation_id=cid)
        event = _event(
            EnumPrLifecyclePhase.IDLE,
            EnumPrLifecycleEventTrigger.INVENTORY_COMPLETE,
            cid,
        )

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.IDLE
        assert len(intents) == 0

    # ------------------------------------------------------------------ #
    # Entry flag: dry_run                                                  #
    # ------------------------------------------------------------------ #

    def test_dry_run_suppresses_side_effect_intents(self) -> None:
        """dry_run=True suppresses START_INVENTORY, START_FIX, START_MERGE intents."""
        handler = HandlerPrLifecycleStateReducer()
        flags = ModelPrLifecycleEntryFlags(dry_run=True)
        state = _state(entry_flags=flags)
        cid = state.correlation_id
        event = _event(
            EnumPrLifecyclePhase.IDLE,
            EnumPrLifecycleEventTrigger.START_RECEIVED,
            cid,
        )

        new_state, intents = handler.delta(state, event)

        # Transition still occurs
        assert new_state.phase == EnumPrLifecyclePhase.INVENTORYING
        # But side-effect intent is suppressed
        assert len(intents) == 0

    def test_dry_run_does_not_suppress_sweep_complete(self) -> None:
        """dry_run=True does NOT suppress SWEEP_COMPLETE intent."""
        handler = HandlerPrLifecycleStateReducer()
        flags = ModelPrLifecycleEntryFlags(dry_run=True)
        cid = uuid4()
        state = _state(
            phase=EnumPrLifecyclePhase.MERGING,
            correlation_id=cid,
            entry_flags=flags,
            prs_inventoried=3,
        )
        event = _event(
            EnumPrLifecyclePhase.MERGING,
            EnumPrLifecycleEventTrigger.MERGE_COMPLETE,
            cid,
        )

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.COMPLETE
        assert len(intents) == 1
        assert intents[0].intent_type == EnumPrLifecycleIntentType.SWEEP_COMPLETE

    def test_dry_run_does_not_suppress_sweep_failed(self) -> None:
        """dry_run=True does NOT suppress SWEEP_FAILED intent."""
        handler = HandlerPrLifecycleStateReducer()
        flags = ModelPrLifecycleEntryFlags(dry_run=True)
        cid = uuid4()
        state = _state(
            phase=EnumPrLifecyclePhase.INVENTORYING,
            correlation_id=cid,
            entry_flags=flags,
        )
        event = _event(
            EnumPrLifecyclePhase.INVENTORYING,
            EnumPrLifecycleEventTrigger.ERROR,
            cid,
            success=False,
            error_message="dry run error",
        )

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.FAILED
        assert len(intents) == 1
        assert intents[0].intent_type == EnumPrLifecycleIntentType.SWEEP_FAILED

    # ------------------------------------------------------------------ #
    # Entry flag: inventory_only                                           #
    # ------------------------------------------------------------------ #

    def test_inventory_only_allows_idle_to_inventorying(self) -> None:
        """inventory_only allows IDLE -> INVENTORYING."""
        handler = HandlerPrLifecycleStateReducer()
        flags = ModelPrLifecycleEntryFlags(inventory_only=True)
        state = _state(entry_flags=flags)
        event = _event(
            EnumPrLifecyclePhase.IDLE,
            EnumPrLifecycleEventTrigger.START_RECEIVED,
            state.correlation_id,
        )

        new_state, _ = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.INVENTORYING

    def test_inventory_only_allows_inventorying_to_triaged(self) -> None:
        """inventory_only allows INVENTORYING -> TRIAGED."""
        handler = HandlerPrLifecycleStateReducer()
        flags = ModelPrLifecycleEntryFlags(inventory_only=True)
        cid = uuid4()
        state = _state(
            phase=EnumPrLifecyclePhase.INVENTORYING,
            correlation_id=cid,
            entry_flags=flags,
        )
        event = _event(
            EnumPrLifecyclePhase.INVENTORYING,
            EnumPrLifecycleEventTrigger.INVENTORY_COMPLETE,
            cid,
        )

        new_state, _ = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.TRIAGED

    def test_inventory_only_blocks_triaged_to_fixing(self) -> None:
        """inventory_only blocks TRIAGED -> FIXING, transitions to COMPLETE instead."""
        handler = HandlerPrLifecycleStateReducer()
        flags = ModelPrLifecycleEntryFlags(inventory_only=True)
        cid = uuid4()
        state = _state(
            phase=EnumPrLifecyclePhase.TRIAGED,
            correlation_id=cid,
            entry_flags=flags,
        )
        event = _event(
            EnumPrLifecyclePhase.TRIAGED,
            EnumPrLifecycleEventTrigger.FIXES_PENDING,
            cid,
        )

        new_state, _ = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.COMPLETE

    def test_inventory_only_blocks_triaged_to_merging(self) -> None:
        """inventory_only blocks TRIAGED -> MERGING, transitions to COMPLETE instead."""
        handler = HandlerPrLifecycleStateReducer()
        flags = ModelPrLifecycleEntryFlags(inventory_only=True)
        cid = uuid4()
        state = _state(
            phase=EnumPrLifecyclePhase.TRIAGED,
            correlation_id=cid,
            entry_flags=flags,
        )
        event = _event(
            EnumPrLifecyclePhase.TRIAGED,
            EnumPrLifecycleEventTrigger.NO_FIXES_NEEDED,
            cid,
        )

        new_state, _ = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.COMPLETE

    # ------------------------------------------------------------------ #
    # Entry flag: fix_only                                                 #
    # ------------------------------------------------------------------ #

    def test_fix_only_allows_triaged_to_fixing(self) -> None:
        """fix_only allows TRIAGED -> FIXING."""
        handler = HandlerPrLifecycleStateReducer()
        flags = ModelPrLifecycleEntryFlags(fix_only=True)
        cid = uuid4()
        state = _state(
            phase=EnumPrLifecyclePhase.TRIAGED,
            correlation_id=cid,
            entry_flags=flags,
        )
        event = _event(
            EnumPrLifecyclePhase.TRIAGED,
            EnumPrLifecycleEventTrigger.FIXES_PENDING,
            cid,
        )

        new_state, _ = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.FIXING

    def test_fix_only_blocks_triaged_to_merging(self) -> None:
        """fix_only blocks TRIAGED -> MERGING (no_fixes_needed path), -> COMPLETE."""
        handler = HandlerPrLifecycleStateReducer()
        flags = ModelPrLifecycleEntryFlags(fix_only=True)
        cid = uuid4()
        state = _state(
            phase=EnumPrLifecyclePhase.TRIAGED,
            correlation_id=cid,
            entry_flags=flags,
        )
        event = _event(
            EnumPrLifecyclePhase.TRIAGED,
            EnumPrLifecycleEventTrigger.NO_FIXES_NEEDED,
            cid,
        )

        new_state, _ = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.COMPLETE

    def test_fix_only_blocks_fixing_to_merging(self) -> None:
        """fix_only blocks FIXING -> MERGING, transitions to COMPLETE instead."""
        handler = HandlerPrLifecycleStateReducer()
        flags = ModelPrLifecycleEntryFlags(fix_only=True)
        cid = uuid4()
        state = _state(
            phase=EnumPrLifecyclePhase.FIXING,
            correlation_id=cid,
            entry_flags=flags,
        )
        event = _event(
            EnumPrLifecyclePhase.FIXING,
            EnumPrLifecycleEventTrigger.FIXES_COMPLETE,
            cid,
        )

        new_state, _ = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.COMPLETE

    def test_fix_only_error_transitions_always_allowed(self) -> None:
        """fix_only does not block error transitions."""
        handler = HandlerPrLifecycleStateReducer()
        flags = ModelPrLifecycleEntryFlags(fix_only=True)
        cid = uuid4()
        state = _state(
            phase=EnumPrLifecyclePhase.FIXING,
            correlation_id=cid,
            entry_flags=flags,
        )
        event = _event(
            EnumPrLifecyclePhase.FIXING,
            EnumPrLifecycleEventTrigger.ERROR,
            cid,
            success=False,
            error_message="fix error",
        )

        new_state, _ = handler.delta(state, event)

        assert new_state.phase == EnumPrLifecyclePhase.FAILED

    # ------------------------------------------------------------------ #
    # handle() protocol shim                                               #
    # ------------------------------------------------------------------ #

    def test_handle_dict_shim_idle_to_inventorying(self) -> None:
        """handle_dict() shim correctly routes to delta()."""
        handler = HandlerPrLifecycleStateReducer()
        cid = str(uuid4())
        input_data = {
            "state": {
                "correlation_id": cid,
                "phase": "idle",
            },
            "event": {
                "correlation_id": cid,
                "source_phase": "idle",
                "trigger": "start_received",
                "success": True,
                "timestamp": datetime.now(tz=UTC).isoformat(),
            },
        }

        result = handler.handle_dict(input_data)

        assert result["state"]["phase"] == "inventorying"
        assert len(result["intents"]) == 1
        assert result["intents"][0]["intent_type"] == "pr_lifecycle.start_inventory"

    # ------------------------------------------------------------------ #
    # EventBusInmemory wiring                                              #
    # ------------------------------------------------------------------ #

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler transitions can be wired through EventBusInmemory."""
        handler = HandlerPrLifecycleStateReducer()
        completed_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            result = handler.handle_dict(payload)
            completed_events.append(result)
            await event_bus.publish(
                RESULT_TOPIC,
                key=None,
                value=json.dumps(result).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-pr-lifecycle-reducer"
        )

        cid = str(uuid4())
        cmd_payload = json.dumps(
            {
                "state": {
                    "correlation_id": cid,
                    "phase": "idle",
                },
                "event": {
                    "correlation_id": cid,
                    "source_phase": "idle",
                    "trigger": "start_received",
                    "success": True,
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                },
            }
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["state"]["phase"] == "inventorying"
        assert len(completed_events[0]["intents"]) == 1

        await event_bus.close()
