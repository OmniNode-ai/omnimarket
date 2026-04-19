# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-node unit tests for the 4 ledger nodes (OMN-8947..OMN-8950).

These verify each node in isolation against its ONEX contract obligations:
- Orchestrator: emits exactly one append-command event, no result
- Effect: writes journal line, emits appended event, real side-effect observable
- Compute: pure hash of journal file, no side effects, no bus publishes
- Reducer: pure delta, returns dict convention with state + intents
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.events.ledger import ModelLedgerAppendedEvent
from omnimarket.nodes.node_ledger_append_effect.handlers.handler_ledger_append import (
    HandlerLedgerAppend,
)
from omnimarket.nodes.node_ledger_hash_compute.handlers.handler_ledger_hash import (
    HandlerLedgerHashCompute,
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
from omnimarket.nodes.node_ledger_state_reducer.models.model_ledger_state import (
    ModelLedgerState,
)


@pytest.fixture
def isolated_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ONEX_STATE_ROOT at a fresh tmp dir for tests that touch journal."""
    monkeypatch.setenv("ONEX_STATE_ROOT", str(tmp_path))
    return tmp_path


# ----------------------------------------------------------------------
# Task 4 (OMN-8947): ORCHESTRATOR
# ----------------------------------------------------------------------


def test_orchestrator_emits_exactly_one_append_command() -> None:
    """Orchestrator: emits events=(ModelLedgerAppendCommand(...),), no result."""
    handler = HandlerLedgerOrchestrator()
    correlation_id = uuid4()
    tick = ModelLedgerTickCommand(tick_id="tick-001", correlation_id=correlation_id)

    output = handler.handle(tick)

    assert len(output.events) == 1
    emitted = output.events[0]
    assert isinstance(emitted, ModelLedgerAppendCommand)
    assert emitted.tick_id == "tick-001"
    assert emitted.correlation_id == correlation_id
    # Correlation is carried forward from the input.
    assert output.correlation_id == correlation_id
    # ORCHESTRATOR outputs have no result payload.
    assert output.result is None


# ----------------------------------------------------------------------
# Task 5 (OMN-8948): EFFECT
# ----------------------------------------------------------------------


def test_effect_appends_line_and_emits_event(isolated_state_root: Path) -> None:
    """Effect: writes ONE line to journal, emits appended event with line_number=1."""
    handler = HandlerLedgerAppend()
    cmd = ModelLedgerAppendCommand(tick_id="tick-abc", correlation_id=uuid4())

    output = handler.handle(cmd)

    journal = isolated_state_root / "ledger-journal.txt"
    assert journal.exists()
    assert journal.read_text(encoding="utf-8") == "tick-abc\n"

    assert len(output.events) == 1
    evt = output.events[0]
    assert isinstance(evt, ModelLedgerAppendedEvent)
    assert evt.tick_id == "tick-abc"
    assert evt.line_number == 1
    assert evt.line_content == "tick-abc"


def test_effect_two_invocations_grow_journal(isolated_state_root: Path) -> None:
    """Two calls → two lines; line_number is authoritative per-call."""
    handler = HandlerLedgerAppend()

    out1 = handler.handle(ModelLedgerAppendCommand(tick_id="a", correlation_id=uuid4()))
    out2 = handler.handle(ModelLedgerAppendCommand(tick_id="b", correlation_id=uuid4()))

    journal = isolated_state_root / "ledger-journal.txt"
    assert journal.read_text(encoding="utf-8") == "a\nb\n"
    assert out1.events[0].line_number == 1
    assert out2.events[0].line_number == 2


# ----------------------------------------------------------------------
# Task 6 (OMN-8949): COMPUTE
# ----------------------------------------------------------------------


def test_compute_is_pure_hash_of_journal(isolated_state_root: Path) -> None:
    """Compute: pure sha256 of journal file. No writes, no bus publishes."""
    # Pre-populate journal manually — compute only reads.
    journal = isolated_state_root / "ledger-journal.txt"
    journal.write_text("line1\nline2\n", encoding="utf-8")

    handler = HandlerLedgerHashCompute()
    appended = ModelLedgerAppendedEvent(
        tick_id="tick-xyz",
        correlation_id=uuid4(),
        line_number=2,
        line_content="line2",
    )
    result = handler.handle(appended)

    # Field-level assertions on result.
    expected_hash = hashlib.sha256(b"line1\nline2\n").hexdigest()
    assert result.tick_id == "tick-xyz"
    assert result.line_count == 2
    assert result.sha256_hex == expected_hash
    assert len(result.sha256_hex) == 64

    # Purity check: journal content unchanged by compute.
    assert journal.read_text(encoding="utf-8") == "line1\nline2\n"


# ----------------------------------------------------------------------
# Task 7 (OMN-8950): REDUCER
# ----------------------------------------------------------------------


def test_reducer_delta_is_pure_and_increments_count() -> None:
    """Reducer.delta() is pure — no env, no file writes, no bus calls.

    Two sequential invocations produce tick_count=2 via delta(). Each
    invocation emits exactly one ``ModelPersistStateIntent`` (OMN-9009 /
    epic OMN-9006: persistence is an effect, reducer declares the intent).
    ``emitted_at`` and ``intent_id`` are injected to preserve determinism.
    """
    from datetime import UTC, datetime

    from omnibase_core.models.intents import ModelPersistStateIntent

    handler = HandlerLedgerStateReducer()
    correlation_id = uuid4()

    evt1 = _make_hash_event("t1", correlation_id, 1, "hash1")
    evt2 = _make_hash_event("t2", correlation_id, 2, "hash2")

    ts = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
    state1, intents1 = handler.delta(
        ModelLedgerState(), evt1, emitted_at=ts, intent_id=uuid4()
    )
    state2, intents2 = handler.delta(state1, evt2, emitted_at=ts, intent_id=uuid4())

    assert state1.tick_count == 1
    assert state1.last_hash == "hash1"
    assert state1.last_line_count == 1
    assert state2.tick_count == 2
    assert state2.last_hash == "hash2"
    assert state2.last_line_count == 2
    assert len(intents1) == 1
    assert isinstance(intents1[0], ModelPersistStateIntent)
    assert len(intents2) == 1
    assert isinstance(intents2[0], ModelPersistStateIntent)


def test_reducer_handle_returns_dict_convention() -> None:
    """handle() wraps delta() output in `{"state": ..., "intents": ...}` shape.

    Matches the real reducer convention at
    node_loop_state_reducer/handlers/handler_loop_state.py:96-110.
    """
    handler = HandlerLedgerStateReducer()
    evt = _make_hash_event("t1", uuid4(), 1, "somehash")

    output = handler.handle(evt)

    assert isinstance(output, dict)
    assert "state" in output
    assert "intents" in output
    assert isinstance(output["intents"], list)
    assert output["state"]["tick_count"] == 1
    assert output["state"]["last_hash"] == "somehash"


def _make_hash_event(tick_id: str, correlation_id, line_count: int, sha: str):
    """Helper to build a ModelLedgerHashComputed."""
    from omnimarket.events.ledger import ModelLedgerHashComputed

    return ModelLedgerHashComputed(
        tick_id=tick_id,
        correlation_id=correlation_id,
        line_count=line_count,
        sha256_hex=sha,
    )
