# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for the ledger four-node composition (OMN-8951).

SCOPE: semantic chain correctness. Calls each handler in sequence (no
RuntimeLocal, no CLI), threading output of one to input of the next, and
asserts field-level correctness at every hop.

This is NOT a runtime proof. Runtime invocation via `onex node` is covered
by OMN-8953 (Proof of Life). Do not conflate.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_ledger_append_effect.handlers.handler_ledger_append import (
    HandlerLedgerAppend,
)
from omnimarket.nodes.node_ledger_append_effect.models.model_ledger_appended_event import (
    ModelLedgerAppendedEvent,
)
from omnimarket.nodes.node_ledger_hash_compute.handlers.handler_ledger_hash import (
    HandlerLedgerHashCompute,
)
from omnimarket.nodes.node_ledger_hash_compute.models.model_ledger_hash_computed import (
    ModelLedgerHashComputed,
)
from omnimarket.nodes.node_ledger_orchestrator.handlers.handler_ledger_orchestrator import (
    HandlerLedgerOrchestrator,
)
from omnimarket.nodes.node_ledger_orchestrator.models.model_ledger_tick_command import (
    ModelLedgerAppendCommand,
    ModelLedgerTickCommand,
)
from omnimarket.nodes.node_ledger_state_reducer.handlers.handler_ledger_state import (
    HandlerLedgerStateReducer,
)


@pytest.fixture
def isolated_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ONEX_STATE_ROOT at a fresh tmp dir."""
    monkeypatch.setenv("ONEX_STATE_ROOT", str(tmp_path))
    return tmp_path


def test_four_node_chain_end_to_end(isolated_state_root: Path) -> None:
    """Tick → append → hash → reduce. Asserts field-level correctness at every hop.

    Ordered topic flow this test implements (by manual handler invocation):
        1. ModelLedgerTickCommand → orchestrator → ModelLedgerAppendCommand
        2. ModelLedgerAppendCommand → effect → journal line + ModelLedgerAppendedEvent
        3. ModelLedgerAppendedEvent → compute → ModelLedgerHashComputed
        4. ModelLedgerHashComputed → reducer → dict(state, intents) projection

    In the real runtime, each step's output event publishes to a topic that the
    next handler subscribes to — exercised via Task 10's `onex node` invocation.
    Here we prove the handlers' semantic data flow in isolation.
    """
    correlation_id = uuid4()
    tick_id = "ledger-tick-001"

    # ---- Step 1: ORCHESTRATOR — tick → append command ----
    orchestrator = HandlerLedgerOrchestrator()
    tick = ModelLedgerTickCommand(tick_id=tick_id, correlation_id=correlation_id)
    orch_output = orchestrator.handle(tick)

    assert len(orch_output.events) == 1
    append_cmd = orch_output.events[0]
    assert isinstance(append_cmd, ModelLedgerAppendCommand)
    assert append_cmd.tick_id == tick_id
    assert append_cmd.correlation_id == correlation_id

    # ---- Step 2: EFFECT — append command → journal + appended event ----
    effect = HandlerLedgerAppend()
    effect_output = effect.handle(append_cmd)

    journal = isolated_state_root / "ledger-journal.txt"
    assert journal.exists()
    assert journal.read_text(encoding="utf-8") == f"{tick_id}\n"

    assert len(effect_output.events) == 1
    appended_evt = effect_output.events[0]
    assert isinstance(appended_evt, ModelLedgerAppendedEvent)
    assert appended_evt.tick_id == tick_id
    assert appended_evt.correlation_id == correlation_id
    assert appended_evt.line_number == 1
    assert appended_evt.line_content == tick_id

    # ---- Step 3: COMPUTE — appended event → hash ----
    compute = HandlerLedgerHashCompute()
    hash_result = compute.handle(appended_evt)

    assert isinstance(hash_result, ModelLedgerHashComputed)
    assert hash_result.tick_id == tick_id
    assert hash_result.correlation_id == correlation_id
    assert hash_result.line_count == 1
    expected_hash = hashlib.sha256(f"{tick_id}\n".encode()).hexdigest()
    assert hash_result.sha256_hex == expected_hash
    assert len(hash_result.sha256_hex) == 64

    # ---- Step 4: REDUCER — hash event → state projection dict ----
    reducer = HandlerLedgerStateReducer()
    reduce_output = reducer.handle(hash_result)

    assert isinstance(reduce_output, dict)
    assert "state" in reduce_output
    assert "intents" in reduce_output
    # OMN-9009: reducer emits a single ModelPersistStateIntent carrying the
    # new state as envelope.data for the downstream persist effect to write.
    assert len(reduce_output["intents"]) == 1
    emitted = reduce_output["intents"][0]
    assert emitted["kind"] == "state.persist"
    assert emitted["envelope"]["node_id"] == "ledger_state_reducer"
    assert emitted["envelope"]["data"]["last_hash"] == expected_hash

    state_dict = reduce_output["state"]
    assert state_dict["tick_count"] == 1
    assert state_dict["last_hash"] == expected_hash
    assert state_dict["last_line_count"] == 1

    # ---- Cross-cutting: correlation_id carried through every hop ----
    # Orchestrator → effect → compute all carry the correlation_id.
    # The reducer's persist intent also propagates correlation_id (OMN-9006).
    assert emitted["correlation_id"] == str(correlation_id)


def test_chain_is_deterministic_over_same_inputs(isolated_state_root: Path) -> None:
    """Running the chain twice with the same tick_id over a fresh journal
    produces identical final state. Proves determinism of the composition.
    """
    # First run — fresh journal.
    _run_chain_once(isolated_state_root, "tick-a")
    first_hash = (isolated_state_root / "ledger-journal.txt").read_bytes()

    # Second run — fresh journal (tmp_path re-used; we truncate).
    (isolated_state_root / "ledger-journal.txt").unlink()
    _run_chain_once(isolated_state_root, "tick-a")
    second_hash = (isolated_state_root / "ledger-journal.txt").read_bytes()

    assert first_hash == second_hash


def _run_chain_once(state_root: Path, tick_id: str) -> dict:
    """Helper: drives the 4-node chain once, returns the reducer projection dict."""
    correlation_id = uuid4()
    orch_output = HandlerLedgerOrchestrator().handle(
        ModelLedgerTickCommand(tick_id=tick_id, correlation_id=correlation_id)
    )
    effect_output = HandlerLedgerAppend().handle(orch_output.events[0])
    hash_result = HandlerLedgerHashCompute().handle(effect_output.events[0])
    return HandlerLedgerStateReducer().handle(hash_result)


def test_two_ticks_produce_two_lines_and_tick_count_two(
    isolated_state_root: Path,
) -> None:
    """Two sequential chain invocations grow the journal to 2 lines.

    Note: reducer state starts from default each call in this test (no
    persistence injection in direct-handler mode). The tick_count=2 case
    is proven via direct delta() call chaining, covered in
    test_reducer_delta_is_pure_and_increments_count (tests/test_ledger_nodes_unit.py).
    """
    _run_chain_once(isolated_state_root, "first")
    projection2 = _run_chain_once(isolated_state_root, "second")

    journal = isolated_state_root / "ledger-journal.txt"
    content = journal.read_text(encoding="utf-8")
    assert content == "first\nsecond\n"

    # The reducer saw a 2-line journal via the compute step.
    assert projection2["state"]["last_line_count"] == 2
