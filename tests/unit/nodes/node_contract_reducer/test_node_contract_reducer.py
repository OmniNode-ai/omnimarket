# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerContractReducer — pure state machine reducer."""

from __future__ import annotations

import inspect

import pytest

from omnimarket.nodes.node_contract_reducer.handlers import handler_contract_reducer
from omnimarket.nodes.node_contract_reducer.handlers.handler_contract_reducer import (
    HandlerContractReducer,
)
from omnimarket.nodes.node_contract_reducer.models.model_reducer_result import (
    ModelReducerResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

LINEAR_TRANSITIONS = [
    {"from": "step_a", "responses": {"ok": {"next": "step_b"}}},
    {"from": "step_b", "responses": {"ok": {"next": "step_c"}}},
    {"from": "step_c", "responses": {"ok": {"next": "done"}}},
]

BRANCHING_TRANSITIONS = [
    {
        "from": "step_a",
        "responses": {
            "cloud": {"next": "step_cloud"},
            "local": {"next": "step_local"},
        },
    },
    {"from": "step_cloud", "responses": {"ok": {"next": "done"}}},
    {"from": "step_local", "responses": {"ok": {"next": "done"}}},
]

SET_STATE_TRANSITIONS = [
    {
        "from": "step_a",
        "responses": {
            "yes": {
                "next": "step_b",
                "set_state": {"confirmed": True, "plan": "basic"},
            },
        },
    },
    {"from": "step_b", "responses": {"ok": {"next": "done"}}},
]


# ---------------------------------------------------------------------------
# Linear flow
# ---------------------------------------------------------------------------


class TestLinearFlow:
    def test_step_a_to_b(self) -> None:
        h = HandlerContractReducer()
        result = h.reduce({"current_step": "step_a"}, "ok", LINEAR_TRANSITIONS)
        assert result.next_step == "step_b"
        assert result.updated_state["current_step"] == "step_b"
        assert not result.is_terminal

    def test_step_b_to_c(self) -> None:
        h = HandlerContractReducer()
        result = h.reduce({"current_step": "step_b"}, "ok", LINEAR_TRANSITIONS)
        assert result.next_step == "step_c"
        assert not result.is_terminal

    def test_step_c_to_done_is_terminal(self) -> None:
        h = HandlerContractReducer()
        result = h.reduce({"current_step": "step_c"}, "ok", LINEAR_TRANSITIONS)
        assert result.next_step == "done"
        assert result.is_terminal


# ---------------------------------------------------------------------------
# Branching
# ---------------------------------------------------------------------------


class TestBranchingFlow:
    def test_cloud_branch(self) -> None:
        h = HandlerContractReducer()
        result = h.reduce({"current_step": "step_a"}, "cloud", BRANCHING_TRANSITIONS)
        assert result.next_step == "step_cloud"
        assert not result.is_terminal

    def test_local_branch(self) -> None:
        h = HandlerContractReducer()
        result = h.reduce({"current_step": "step_a"}, "local", BRANCHING_TRANSITIONS)
        assert result.next_step == "step_local"
        assert not result.is_terminal

    def test_branch_terminal(self) -> None:
        h = HandlerContractReducer()
        result = h.reduce({"current_step": "step_cloud"}, "ok", BRANCHING_TRANSITIONS)
        assert result.is_terminal


# ---------------------------------------------------------------------------
# Terminal detection
# ---------------------------------------------------------------------------


class TestTerminalDetection:
    def test_done_is_always_terminal(self) -> None:
        h = HandlerContractReducer()
        transitions = [{"from": "only", "responses": {"go": {"next": "done"}}}]
        result = h.reduce({"current_step": "only"}, "go", transitions)
        assert result.is_terminal

    def test_step_with_no_outgoing_transition_is_terminal(self) -> None:
        """next_step not in {t['from'] for t in transitions} => terminal."""
        h = HandlerContractReducer()
        transitions = [{"from": "entry", "responses": {"go": {"next": "leaf"}}}]
        result = h.reduce({"current_step": "entry"}, "go", transitions)
        assert result.next_step == "leaf"
        assert result.is_terminal


# ---------------------------------------------------------------------------
# Invalid response handling
# ---------------------------------------------------------------------------


class TestInvalidResponse:
    def test_unknown_response_raises_value_error(self) -> None:
        h = HandlerContractReducer()
        with pytest.raises(ValueError, match="not valid for step"):
            h.reduce({"current_step": "step_a"}, "bogus", LINEAR_TRANSITIONS)

    def test_no_transition_for_step_raises_value_error(self) -> None:
        h = HandlerContractReducer()
        with pytest.raises(ValueError, match="No transition declared"):
            h.reduce({"current_step": "missing_step"}, "ok", LINEAR_TRANSITIONS)

    def test_missing_current_step_raises_key_error(self) -> None:
        h = HandlerContractReducer()
        with pytest.raises(KeyError):
            h.reduce({}, "ok", LINEAR_TRANSITIONS)


# ---------------------------------------------------------------------------
# State accumulation
# ---------------------------------------------------------------------------


class TestStateAccumulation:
    def test_set_state_merges_into_state(self) -> None:
        h = HandlerContractReducer()
        result = h.reduce(
            {"current_step": "step_a", "existing": "value"},
            "yes",
            SET_STATE_TRANSITIONS,
        )
        assert result.updated_state["confirmed"] is True
        assert result.updated_state["plan"] == "basic"
        assert result.updated_state["existing"] == "value"
        assert result.updated_state["current_step"] == "step_b"

    def test_existing_state_not_clobbered_by_unrelated_transition(self) -> None:
        h = HandlerContractReducer()
        result = h.reduce(
            {"current_step": "step_a", "user_name": "alice"},
            "ok",
            LINEAR_TRANSITIONS,
        )
        assert result.updated_state["user_name"] == "alice"

    def test_set_state_overrides_existing_key(self) -> None:
        h = HandlerContractReducer()
        transitions = [
            {
                "from": "s1",
                "responses": {"go": {"next": "done", "set_state": {"x": "new"}}},
            }
        ]
        result = h.reduce({"current_step": "s1", "x": "old"}, "go", transitions)
        assert result.updated_state["x"] == "new"


# ---------------------------------------------------------------------------
# handle() dict wrapper
# ---------------------------------------------------------------------------


class TestHandleWrapper:
    def test_handle_returns_dict(self) -> None:
        h = HandlerContractReducer()
        out = h.handle(
            {
                "current_state": {"current_step": "step_a"},
                "user_response": "ok",
                "contract_transitions": LINEAR_TRANSITIONS,
            }
        )
        assert isinstance(out, dict)
        assert out["next_step"] == "step_b"
        assert out["is_terminal"] is False

    def test_handle_result_matches_reduce(self) -> None:
        h = HandlerContractReducer()
        direct = h.reduce({"current_step": "step_c"}, "ok", LINEAR_TRANSITIONS)
        via_handle = h.handle(
            {
                "current_state": {"current_step": "step_c"},
                "user_response": "ok",
                "contract_transitions": LINEAR_TRANSITIONS,
            }
        )
        assert via_handle == direct.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Purity guard
# ---------------------------------------------------------------------------


class TestReducerIsPure:
    @pytest.mark.parametrize(
        "forbidden",
        [
            "ProtocolEventBus",
            "ProtocolStateStore",
            "state_store",
            "kafka",
            "asyncio",
        ],
    )
    def test_handler_source_has_no_io_references(self, forbidden: str) -> None:
        source = inspect.getsource(handler_contract_reducer)
        assert forbidden not in source, (
            f"Pure compute node must not reference {forbidden!r}."
        )

    def test_result_is_typed_model(self) -> None:
        h = HandlerContractReducer()
        result = h.reduce({"current_step": "step_a"}, "ok", LINEAR_TRANSITIONS)
        assert isinstance(result, ModelReducerResult)
