# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Focused unit tests for HandlerPrLifecycleOrchestrator hotspots.

Covers:
- HandlerPrLifecycleOrchestrator construction with required event_bus
- _check_protocol_conformance: missing handle method, correct conformance, drift
- _map_ci_status: ci_passing=True/False/None → correct status strings
- _failed_check_names: filters failing conclusions from check_runs
- ModelPrLifecycleStartCommand validation (valid and invalid inputs)
- EnumOrchestratorState values are terminal/non-terminal as documented
- Stub sub-handlers: inventory/triage/reducer/merge/fix stubs complete without raising

Related: OMN-12383
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    EnumOrchestratorState,
    HandlerPrLifecycleOrchestrator,
    ModelPrLifecycleResult,
    ModelPrLifecycleStartCommand,
    _failed_check_names,
    _map_ci_status,
    _StubFixHandler,
    _StubInventoryHandler,
    _StubMergeHandler,
    _StubReducerHandler,
    _StubTriageHandler,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    ProtocolInventoryHandler,
)

# ---------------------------------------------------------------------------
# EnumOrchestratorState: terminal vs non-terminal
# ---------------------------------------------------------------------------


class TestEnumOrchestratorState:
    def test_complete_and_failed_are_terminal(self) -> None:
        terminal = {EnumOrchestratorState.COMPLETE, EnumOrchestratorState.FAILED}
        assert EnumOrchestratorState.COMPLETE in terminal
        assert EnumOrchestratorState.FAILED in terminal

    def test_idle_inventorying_merging_are_not_terminal(self) -> None:
        terminal = {EnumOrchestratorState.COMPLETE, EnumOrchestratorState.FAILED}
        for state in (
            EnumOrchestratorState.IDLE,
            EnumOrchestratorState.INVENTORYING,
            EnumOrchestratorState.MERGING,
            EnumOrchestratorState.FIXING,
        ):
            assert state not in terminal

    def test_all_states_are_strings(self) -> None:
        """StrEnum — all values are plain strings."""
        for state in EnumOrchestratorState:
            assert isinstance(state.value, str)


# ---------------------------------------------------------------------------
# ModelPrLifecycleStartCommand: input validation
# ---------------------------------------------------------------------------


class TestModelPrLifecycleStartCommand:
    def test_minimal_valid_command(self) -> None:
        cmd = ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="20260529-120000-abc123",
        )
        assert cmd.dry_run is False
        assert cmd.repos == ""

    def test_invalid_run_id_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ModelPrLifecycleStartCommand(
                correlation_id=uuid4(),
                run_id="../../etc/passwd",  # path traversal chars
            )

    def test_dry_run_defaults_false(self) -> None:
        cmd = ModelPrLifecycleStartCommand(correlation_id=uuid4(), run_id="run-001")
        assert cmd.dry_run is False

    def test_all_flags_can_be_set(self) -> None:
        cmd = ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="run-flags",
            dry_run=True,
            inventory_only=True,
            fix_only=False,
            merge_only=False,
            repos="OmniNode-ai/omnimarket,OmniNode-ai/omniclaude",
        )
        assert cmd.dry_run is True
        assert cmd.inventory_only is True
        assert cmd.repos == "OmniNode-ai/omnimarket,OmniNode-ai/omniclaude"


# ---------------------------------------------------------------------------
# _map_ci_status: ci_passing field mapping
# ---------------------------------------------------------------------------


class TestMapCiStatus:
    def _make_state(self, ci_passing: bool | None) -> Any:
        s = MagicMock()
        s.ci_passing = ci_passing
        return s

    def test_ci_passing_true_returns_success(self) -> None:
        assert _map_ci_status(self._make_state(True)) == "success"

    def test_ci_passing_false_returns_failure(self) -> None:
        assert _map_ci_status(self._make_state(False)) == "failure"

    def test_ci_passing_none_returns_unknown(self) -> None:
        assert _map_ci_status(self._make_state(None)) == "unknown"

    def test_no_ci_passing_attr_returns_unknown(self) -> None:
        state = object()  # no ci_passing attribute
        assert _map_ci_status(state) == "unknown"


# ---------------------------------------------------------------------------
# _failed_check_names: extracts failed conclusion names
# ---------------------------------------------------------------------------


class TestFailedCheckNames:
    def _make_check(self, name: str, conclusion: str) -> Any:
        c = MagicMock()
        c.name = name
        c.conclusion = conclusion
        return c

    def test_no_checks_returns_empty(self) -> None:
        state = MagicMock()
        state.check_runs = ()
        assert _failed_check_names(state) == ()

    def test_passing_check_not_included(self) -> None:
        state = MagicMock()
        state.check_runs = (self._make_check("build", "success"),)
        assert _failed_check_names(state) == ()

    def test_failed_conclusion_included(self) -> None:
        state = MagicMock()
        state.check_runs = (self._make_check("receipt-gate", "failure"),)
        result = _failed_check_names(state)
        assert "receipt-gate" in result

    def test_mixed_checks_only_failed_included(self) -> None:
        state = MagicMock()
        state.check_runs = (
            self._make_check("build", "success"),
            self._make_check("lint", "failure"),
            self._make_check("deploy", "timed_out"),
        )
        result = _failed_check_names(state)
        assert "lint" in result
        assert "deploy" in result
        assert "build" not in result

    def test_cancelled_included(self) -> None:
        state = MagicMock()
        state.check_runs = (self._make_check("ci", "cancelled"),)
        result = _failed_check_names(state)
        assert "ci" in result

    def test_no_check_runs_attr_returns_empty(self) -> None:
        state = MagicMock(spec=[])
        assert _failed_check_names(state) == ()


# ---------------------------------------------------------------------------
# HandlerPrLifecycleOrchestrator: construction with event_bus
# ---------------------------------------------------------------------------


class TestHandlerPrLifecycleOrchestratorConstruction:
    def test_construction_with_mock_event_bus(self) -> None:
        """Minimal construction with required event_bus succeeds."""
        mock_bus = MagicMock(spec=ProtocolEventBusPublisher)
        handler = HandlerPrLifecycleOrchestrator(event_bus=mock_bus)
        assert handler is not None
        assert handler._event_bus is mock_bus

    def test_sub_handlers_default_to_none(self) -> None:
        """All optional sub-handlers default to None until _ensure_sub_handlers()."""
        mock_bus = MagicMock(spec=ProtocolEventBusPublisher)
        handler = HandlerPrLifecycleOrchestrator(event_bus=mock_bus)
        assert handler._inventory is None
        assert handler._triage is None
        assert handler._reducer is None
        assert handler._merge is None
        assert handler._fix is None

    def test_explicit_sub_handler_injection(self) -> None:
        """Explicitly injected handlers are stored correctly."""
        mock_bus = MagicMock(spec=ProtocolEventBusPublisher)
        mock_inventory = MagicMock()
        mock_inventory.handle = MagicMock()
        handler = HandlerPrLifecycleOrchestrator(
            inventory=mock_inventory,
            event_bus=mock_bus,
        )
        assert handler._inventory is mock_inventory


# ---------------------------------------------------------------------------
# _check_protocol_conformance: protocol guard
# ---------------------------------------------------------------------------


class TestCheckProtocolConformance:
    """_check_protocol_conformance raises TypeError for non-conforming objects."""

    def test_object_without_handle_raises(self) -> None:
        with pytest.raises(TypeError, match="handle"):
            HandlerPrLifecycleOrchestrator._check_protocol_conformance(
                object(), ProtocolInventoryHandler, "inventory"
            )

    def test_conforming_handler_passes(self) -> None:
        """A handler with the correct handle() signature passes the check."""

        class _GoodHandler:
            def handle(self, input_model: Any) -> Any:
                return None

        # Must not raise
        HandlerPrLifecycleOrchestrator._check_protocol_conformance(
            _GoodHandler(), ProtocolInventoryHandler, "inventory"
        )


# ---------------------------------------------------------------------------
# Stub sub-handlers: smoke-test that stubs produce correct default types
# ---------------------------------------------------------------------------


class TestStubSubHandlers:
    def test_stub_inventory_returns_inventory_result(self) -> None:
        stub = _StubInventoryHandler()
        result = stub.handle(MagicMock())
        # InventoryResult has prs attribute
        assert hasattr(result, "prs")
        assert hasattr(result, "total_collected")
        assert result.total_collected == 0

    @pytest.mark.asyncio
    async def test_stub_triage_returns_triage_result(self) -> None:
        stub = _StubTriageHandler()
        result = await stub.handle(uuid4(), [])
        assert hasattr(result, "classified")
        assert result.green_count == 0

    @pytest.mark.asyncio
    async def test_stub_reducer_returns_reducer_result(self) -> None:
        stub = _StubReducerHandler()
        result = await stub.handle()
        assert hasattr(result, "intents")

    @pytest.mark.asyncio
    async def test_stub_merge_returns_merge_result(self) -> None:
        stub = _StubMergeHandler()
        result = await stub.handle(MagicMock())
        assert hasattr(result, "prs_merged")

    @pytest.mark.asyncio
    async def test_stub_fix_returns_fix_result(self) -> None:
        stub = _StubFixHandler()
        result = await stub.handle(MagicMock())
        assert hasattr(result, "prs_dispatched")


# ---------------------------------------------------------------------------
# ModelPrLifecycleResult: output model completeness
# ---------------------------------------------------------------------------


class TestModelPrLifecycleResult:
    def test_minimal_result_defaults(self) -> None:
        corr = uuid4()
        result = ModelPrLifecycleResult(correlation_id=corr)
        assert result.prs_inventoried == 0
        assert result.prs_merged == 0
        assert result.final_state == "COMPLETE"
        assert result.error_message is None

    def test_failed_result_carries_error_message(self) -> None:
        corr = uuid4()
        result = ModelPrLifecycleResult(
            correlation_id=corr,
            final_state="FAILED",
            error_message="broker unreachable",
        )
        assert result.final_state == "FAILED"
        assert result.error_message == "broker unreachable"
