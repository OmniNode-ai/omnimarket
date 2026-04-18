# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-9010: HandlerMergeSweepStateReducer emits ModelPersistStateIntent.

Part of pure-reducer epic OMN-9006. The reducer itself was already pure when
landed under OMN-8964 (PR #319) — these tests add the contract that it MUST
also append a typed ``ModelPersistStateIntent`` to the intents list whenever
state mutates, so the downstream ``node_state_persist_effect`` can persist.

Invariants asserted here:
    1. First-write mutates state and appends ONE ModelPersistStateIntent with
       the correct run_id-scoped envelope.
    2. Dedup-skip path (duplicate event) emits NO persist intent and NO
       additional intents — state must be bit-equal and intents empty.
    3. Terminal emission path still emits the existing bus-publish dict intent
       AND a ModelPersistStateIntent in the same call.
    4. The handler performs no I/O — asserted by call tracing the pure
       ``delta`` method (no filesystem, no bus, no env reads).
    5. ``datetime.utcnow()`` is not called by the handler (naive-datetime
       deprecation fix); emitted intents carry tz-aware ``datetime``.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from uuid import UUID

from omnibase_core.models.intents import ModelPersistStateIntent

from omnimarket.nodes.node_merge_sweep_state_reducer.handlers import (
    handler_sweep_state as handler_module,
)
from omnimarket.nodes.node_merge_sweep_state_reducer.handlers.handler_sweep_state import (
    HandlerMergeSweepStateReducer,
)
from omnimarket.nodes.node_merge_sweep_state_reducer.models.model_merge_sweep_state import (
    ModelMergeSweepState,
)
from omnimarket.nodes.node_sweep_outcome_classify.models.model_sweep_outcome import (
    EnumSweepOutcome,
    ModelSweepOutcomeClassified,
)

_RUN_ID = UUID("00000000-0000-4000-a000-000000000001")
_CORR_ID = UUID("00000000-0000-4000-a000-000000000002")


def _event(
    pr_number: int,
    outcome: EnumSweepOutcome,
    total_prs: int = 3,
) -> ModelSweepOutcomeClassified:
    return ModelSweepOutcomeClassified(
        pr_number=pr_number,
        repo="OmniNode-ai/omni_home",
        correlation_id=_CORR_ID,
        run_id=_RUN_ID,
        total_prs=total_prs,
        outcome=outcome,
        source_event_type="armed",
    )


def _persist_intents(
    intents: list[object],
) -> list[ModelPersistStateIntent]:
    return [i for i in intents if isinstance(i, ModelPersistStateIntent)]


def test_first_write_appends_one_persist_intent() -> None:
    handler = HandlerMergeSweepStateReducer()
    state = ModelMergeSweepState(run_id=_RUN_ID, total_prs=3)

    _, intents = handler.delta(state, _event(100, EnumSweepOutcome.ARMED))

    persist = _persist_intents(intents)
    assert len(persist) == 1, f"expected exactly one persist intent, got {intents!r}"
    intent = persist[0]
    assert isinstance(intent, ModelPersistStateIntent)
    assert intent.correlation_id == _CORR_ID
    assert intent.envelope.scope_id == str(_RUN_ID)
    assert intent.envelope.node_id == "node_merge_sweep_state_reducer"
    # Envelope data must carry the mutated state (round-trippable).
    assert intent.envelope.data["armed_count"] == 1
    assert intent.envelope.data["terminal_emitted"] is False


def test_dedup_skip_emits_no_persist_intent() -> None:
    handler = HandlerMergeSweepStateReducer()
    state = ModelMergeSweepState(run_id=_RUN_ID, total_prs=3)

    state_after_first, _ = handler.delta(state, _event(100, EnumSweepOutcome.ARMED))
    state_after_second, intents = handler.delta(
        state_after_first, _event(100, EnumSweepOutcome.ARMED)
    )

    # bit-equal state (no mutation) and no intents at all
    assert state_after_first.model_dump_json() == state_after_second.model_dump_json()
    assert intents == []
    assert _persist_intents(intents) == []


def test_terminal_emits_both_bus_publish_and_persist_intent() -> None:
    handler = HandlerMergeSweepStateReducer()
    state = ModelMergeSweepState(run_id=_RUN_ID, total_prs=2)

    state, _ = handler.delta(state, _event(100, EnumSweepOutcome.ARMED, total_prs=2))
    _, intents_terminal = handler.delta(
        state, _event(200, EnumSweepOutcome.REBASED, total_prs=2)
    )

    # Existing bus-publish dict still present.
    bus_dicts = [i for i in intents_terminal if isinstance(i, dict)]
    assert len(bus_dicts) == 1
    assert "merge-sweep-completed" in bus_dicts[0]["topic"]

    # New: a ModelPersistStateIntent is also appended.
    persist = _persist_intents(intents_terminal)
    assert len(persist) == 1
    assert persist[0].envelope.data["terminal_emitted"] is True


def test_emitted_at_is_timezone_aware() -> None:
    handler = HandlerMergeSweepStateReducer()
    state = ModelMergeSweepState(run_id=_RUN_ID, total_prs=3)
    _, intents = handler.delta(state, _event(100, EnumSweepOutcome.ARMED))
    persist = _persist_intents(intents)
    assert persist, "expected a persist intent"
    # tzinfo non-None is the naive-utc fix; must be UTC.
    assert persist[0].emitted_at.tzinfo is not None
    assert persist[0].emitted_at.utcoffset() == UTC.utcoffset(datetime.now(UTC))


def test_handler_source_does_not_call_utcnow() -> None:
    """Static check: the handler module source must not contain utcnow().

    utcnow() returns naive datetimes and is deprecated in 3.12+. The reducer
    must use datetime.now(timezone.utc) instead.
    """
    src = inspect.getsource(handler_module)
    assert "datetime.utcnow(" not in src, (
        "handler_sweep_state.py must not use datetime.utcnow(); use "
        "datetime.now(timezone.utc) instead"
    )
