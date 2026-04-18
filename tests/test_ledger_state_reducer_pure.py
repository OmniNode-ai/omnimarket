# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure-reducer tests for HandlerLedgerStateReducer [OMN-9009 / epic OMN-9006].

These tests encode the pure-reducer-as-effect contract: the reducer must emit
a typed ``ModelPersistStateIntent`` in its intents list carrying a
``ModelStateEnvelope`` populated from the newly computed state. The reducer
must not import or reference any persistence protocol — persistence is the
concern of ``node_state_persist_effect`` downstream.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.models.intents import ModelPersistStateIntent
from omnibase_core.models.state.model_state_envelope import ModelStateEnvelope

from omnimarket.nodes.node_ledger_hash_compute.models.model_ledger_hash_computed import (
    ModelLedgerHashComputed,
)
from omnimarket.nodes.node_ledger_state_reducer.handlers import handler_ledger_state
from omnimarket.nodes.node_ledger_state_reducer.handlers.handler_ledger_state import (
    HandlerLedgerStateReducer,
)
from omnimarket.nodes.node_ledger_state_reducer.models.model_ledger_state import (
    ModelLedgerState,
)

_FIXED_TS = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)


def _hash_event(
    tick: str = "t1", lines: int = 1, sha: str = "deadbeef"
) -> ModelLedgerHashComputed:
    return ModelLedgerHashComputed(
        tick_id=tick,
        correlation_id=uuid4(),
        line_count=lines,
        sha256_hex=sha,
    )


class TestDeltaEmitsPersistStateIntent:
    """delta() returns a ModelPersistStateIntent carrying new state as envelope.data."""

    def test_delta_returns_exactly_one_persist_state_intent(self) -> None:
        handler = HandlerLedgerStateReducer()
        _, intents = handler.delta(
            ModelLedgerState(),
            _hash_event(),
            emitted_at=_FIXED_TS,
            intent_id=uuid4(),
        )
        assert len(intents) == 1
        intent = intents[0]
        assert isinstance(intent, ModelPersistStateIntent)
        assert intent.kind == "state.persist"

    def test_intent_envelope_wraps_new_state_data(self) -> None:
        handler = HandlerLedgerStateReducer()
        evt = _hash_event(tick="tX", lines=7, sha="abc123")
        new_state, intents = handler.delta(
            ModelLedgerState(),
            evt,
            emitted_at=_FIXED_TS,
            intent_id=uuid4(),
        )
        assert new_state.tick_count == 1
        envelope: ModelStateEnvelope = intents[0].envelope
        assert isinstance(envelope, ModelStateEnvelope)
        assert envelope.node_id == "ledger_state_reducer"
        assert envelope.data == new_state.model_dump(mode="json")
        assert envelope.data["tick_count"] == 1
        assert envelope.data["last_hash"] == "abc123"
        assert envelope.data["last_line_count"] == 7

    def test_intent_correlation_id_matches_event(self) -> None:
        handler = HandlerLedgerStateReducer()
        evt = _hash_event()
        _, intents = handler.delta(
            ModelLedgerState(), evt, emitted_at=_FIXED_TS, intent_id=uuid4()
        )
        assert intents[0].correlation_id == evt.correlation_id

    def test_intent_emitted_at_is_the_injected_timestamp(self) -> None:
        handler = HandlerLedgerStateReducer()
        _, intents = handler.delta(
            ModelLedgerState(),
            _hash_event(),
            emitted_at=_FIXED_TS,
            intent_id=uuid4(),
        )
        assert intents[0].emitted_at == _FIXED_TS
        assert intents[0].envelope.written_at == _FIXED_TS

    def test_delta_is_deterministic_for_identical_inputs(self) -> None:
        """Pure reducer contract: identical (state, event, emitted_at, intent_id)
        must produce byte-identical outputs. Guards replay/idempotence."""
        handler = HandlerLedgerStateReducer()
        evt = _hash_event(tick="tZ", lines=3, sha="cafe")
        fixed_id = uuid4()

        s1, i1 = handler.delta(
            ModelLedgerState(), evt, emitted_at=_FIXED_TS, intent_id=fixed_id
        )
        s2, i2 = handler.delta(
            ModelLedgerState(), evt, emitted_at=_FIXED_TS, intent_id=fixed_id
        )
        assert s1 == s2
        assert i1[0].model_dump(mode="json") == i2[0].model_dump(mode="json")


class TestHandleDictShape:
    """handle() preserves the ``{"state": ..., "intents": [...]}`` convention."""

    def test_handle_output_includes_serialized_intent(self) -> None:
        handler = HandlerLedgerStateReducer()
        out = handler.handle(_hash_event(tick="tY", lines=3, sha="feed"))
        assert isinstance(out, dict)
        assert "intents" in out
        assert isinstance(out["intents"], list)
        assert len(out["intents"]) == 1
        serialized = out["intents"][0]
        assert serialized["kind"] == "state.persist"
        assert serialized["envelope"]["data"]["last_hash"] == "feed"
        assert serialized["envelope"]["node_id"] == "ledger_state_reducer"


class TestReducerIsPure:
    """Reducer module must not reference persistence I/O — that is an effect concern."""

    @pytest.mark.parametrize(
        "forbidden",
        [
            "ProtocolStateStore",
            "state_store",
            "_persist_reducer_projection",
            "ProtocolEventBus",
        ],
    )
    def test_reducer_source_has_no_io_references(self, forbidden: str) -> None:
        code_only = _reducer_code_without_docstrings_or_comments()
        assert forbidden not in code_only, (
            f"Reducer source must not reference {forbidden!r}; persistence belongs in the effect node."
        )

    def test_delta_does_not_call_nondeterministic_builtins(self) -> None:
        """``delta()`` must not call ``datetime.now`` or ``uuid4`` — both are
        non-deterministic and break replay/idempotence. They belong in the
        effect boundary (``handle()`` or the runtime caller)."""
        import ast

        source = inspect.getsource(handler_ledger_state)
        tree = ast.parse(source)
        delta_fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "delta"
        )
        forbidden_calls: list[str] = []
        for node in ast.walk(delta_fn):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "now":
                    forbidden_calls.append("datetime.now")
                if isinstance(node.func, ast.Name) and node.func.id == "uuid4":
                    forbidden_calls.append("uuid4")
        assert not forbidden_calls, (
            f"delta() contains non-deterministic calls: {forbidden_calls}. "
            "Inject emitted_at / intent_id via keyword args instead."
        )


def _reducer_code_without_docstrings_or_comments() -> str:
    """Return the reducer source with all module/class/function docstrings
    stripped via AST so that historical documentation mentions cannot
    satisfy or defeat code-level contract checks."""
    import ast

    source = inspect.getsource(handler_ledger_state)
    tree = ast.parse(source)
    code_only = source
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            ds = ast.get_docstring(node, clean=False)
            if ds:
                code_only = code_only.replace(ds, "")
    return code_only
