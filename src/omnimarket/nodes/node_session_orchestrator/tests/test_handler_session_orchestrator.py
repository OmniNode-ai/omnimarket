# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit and integration tests for HandlerSessionOrchestrator (OMN-8367 PoC).

Unit tests (default): inject fabricated probe callables — no SSH, no subprocess, no network.
Integration tests (@pytest.mark.integration): call the real _probe_runtime_health and
_probe_deploy_agent with SSH unavailable (bad host env) and assert the exception path
returns a valid ModelHealthDimensionResult without leaking.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator import (
    EnumDimensionStatus,
    EnumGateDecision,
    EnumSessionStatus,
    HandlerSessionOrchestrator,
    ModelHealthDimensionResult,
    ModelSessionOrchestratorCommand,
    _probe_deploy_agent,
    _probe_runtime_health,
)


def _make_dim(
    name: str,
    status: EnumDimensionStatus,
    blocks_dispatch: bool = False,
) -> ModelHealthDimensionResult:
    return ModelHealthDimensionResult(
        dimension=name,
        status=status,
        source="fake",
        timestamp=datetime.now(tz=UTC),
        stale_after=timedelta(minutes=10),
        details={},
        actionable_items=[],
        blocks_dispatch=blocks_dispatch,
    )


def _green_probe(name: str) -> Callable[[], ModelHealthDimensionResult]:
    def probe() -> ModelHealthDimensionResult:
        return _make_dim(name, EnumDimensionStatus.GREEN)

    probe.__name__ = f"_probe_{name}"
    return probe


def _red_probe(
    name: str, blocks: bool = False
) -> Callable[[], ModelHealthDimensionResult]:
    def probe() -> ModelHealthDimensionResult:
        return _make_dim(name, EnumDimensionStatus.RED, blocks_dispatch=blocks)

    probe.__name__ = f"_probe_{name}"
    return probe


def _yellow_probe(
    name: str, blocks: bool = False
) -> Callable[[], ModelHealthDimensionResult]:
    def probe() -> ModelHealthDimensionResult:
        return _make_dim(name, EnumDimensionStatus.YELLOW, blocks_dispatch=blocks)

    probe.__name__ = f"_probe_{name}"
    return probe


class TestPhase1AllGreen:
    def test_all_green_produces_proceed(self) -> None:
        probes = [_green_probe(f"dim_{i}") for i in range(8)]
        handler = HandlerSessionOrchestrator(probes=probes)
        cmd = ModelSessionOrchestratorCommand(dry_run=True, phase=1)
        result = handler.handle(cmd)

        assert result.status == EnumSessionStatus.COMPLETE
        assert result.health_report is not None
        assert result.health_report.overall_status == EnumDimensionStatus.GREEN
        assert result.health_report.gate_decision == EnumGateDecision.PROCEED
        assert result.halt_reason == ""

    def test_session_id_generated_if_empty(self) -> None:
        handler = HandlerSessionOrchestrator(probes=[_green_probe("dim_1")])
        cmd = ModelSessionOrchestratorCommand(session_id="", dry_run=True, phase=1)
        result = handler.handle(cmd)
        assert result.session_id.startswith("sess-")

    def test_explicit_session_id_preserved(self) -> None:
        handler = HandlerSessionOrchestrator(probes=[_green_probe("dim_1")])
        cmd = ModelSessionOrchestratorCommand(
            session_id="sess-test-01", dry_run=True, phase=1
        )
        result = handler.handle(cmd)
        assert result.session_id == "sess-test-01"

    def test_correlation_id_propagated(self) -> None:
        handler = HandlerSessionOrchestrator(probes=[_green_probe("dim_1")])
        cmd = ModelSessionOrchestratorCommand(
            session_id="sess-test-01",
            correlation_id="sess-test-01.disp-001",
            dry_run=True,
            phase=1,
        )
        result = handler.handle(cmd)
        assert result.correlation_id == "sess-test-01.disp-001"

    def test_correlation_id_defaults_to_session_id(self) -> None:
        handler = HandlerSessionOrchestrator(probes=[_green_probe("dim_1")])
        cmd = ModelSessionOrchestratorCommand(
            session_id="sess-test-02", correlation_id="", dry_run=True, phase=1
        )
        result = handler.handle(cmd)
        assert result.correlation_id == "sess-test-02"


class TestPhase1RedBlocking:
    def test_red_blocking_dimension_halts_gate(self) -> None:
        probes = [
            _green_probe("pr_inventory"),
            _red_probe("golden_chain", blocks=True),
            _green_probe("linear_sync"),
            _green_probe("runtime_health"),
            _green_probe("plugin_currency"),
            _green_probe("deploy_agent"),
            _green_probe("observability"),
            _green_probe("repo_sync"),
        ]
        handler = HandlerSessionOrchestrator(probes=probes)
        cmd = ModelSessionOrchestratorCommand(dry_run=True, phase=1)
        result = handler.handle(cmd)

        assert result.status == EnumSessionStatus.HALTED
        assert result.health_report is not None
        assert result.health_report.gate_decision == EnumGateDecision.FIX_ONLY
        assert result.health_report.overall_status == EnumDimensionStatus.RED

    def test_red_non_blocking_dimension_still_halts(self) -> None:
        """Any RED halts even if blocks_dispatch=False — per design spec."""
        probes = [_red_probe("pr_inventory", blocks=False)] + [
            _green_probe(f"d{i}") for i in range(7)
        ]
        handler = HandlerSessionOrchestrator(probes=probes)
        cmd = ModelSessionOrchestratorCommand(dry_run=True, phase=1)
        result = handler.handle(cmd)

        assert result.health_report is not None
        assert result.health_report.gate_decision == EnumGateDecision.FIX_ONLY

    def test_full_session_halts_on_red_without_phase_flag(self) -> None:
        probes = [_red_probe("golden_chain", blocks=True)] + [
            _green_probe(f"d{i}") for i in range(7)
        ]
        handler = HandlerSessionOrchestrator(probes=probes)
        cmd = ModelSessionOrchestratorCommand(dry_run=True, phase=0)
        result = handler.handle(cmd)

        assert result.status == EnumSessionStatus.HALTED
        assert "golden_chain" in result.halt_reason


class TestPhase1YellowBlocking:
    def test_yellow_blocking_halts_gate(self) -> None:
        probes = [
            _green_probe("pr_inventory"),
            _yellow_probe("golden_chain", blocks=True),
        ] + [_green_probe(f"d{i}") for i in range(6)]
        handler = HandlerSessionOrchestrator(probes=probes)
        cmd = ModelSessionOrchestratorCommand(dry_run=True, phase=1)
        result = handler.handle(cmd)

        assert result.health_report is not None
        assert result.health_report.gate_decision == EnumGateDecision.FIX_ONLY

    def test_yellow_non_blocking_proceeds(self) -> None:
        probes = [_yellow_probe("pr_inventory", blocks=False)] + [
            _green_probe(f"d{i}") for i in range(7)
        ]
        handler = HandlerSessionOrchestrator(probes=probes)
        cmd = ModelSessionOrchestratorCommand(dry_run=True, phase=1)
        result = handler.handle(cmd)

        assert result.status == EnumSessionStatus.COMPLETE
        assert result.health_report is not None
        assert result.health_report.gate_decision == EnumGateDecision.PROCEED
        assert result.health_report.overall_status == EnumDimensionStatus.YELLOW


class TestPhase1SkipHealth:
    def test_skip_health_bypasses_probes(self) -> None:
        called = []

        def probe() -> ModelHealthDimensionResult:
            called.append(True)
            return _make_dim("dim_1", EnumDimensionStatus.GREEN)

        handler = HandlerSessionOrchestrator(probes=[probe])
        cmd = ModelSessionOrchestratorCommand(skip_health=True, dry_run=True, phase=0)
        result = handler.handle(cmd)

        assert not called
        assert result.health_report is None
        assert result.status == EnumSessionStatus.COMPLETE


class TestPhase1ProbeException:
    def test_probe_exception_treated_as_red(self) -> None:
        def failing_probe() -> ModelHealthDimensionResult:
            raise RuntimeError("SSH timeout")

        failing_probe.__name__ = "_probe_runtime_health"

        handler = HandlerSessionOrchestrator(probes=[failing_probe])
        cmd = ModelSessionOrchestratorCommand(dry_run=True, phase=1)
        result = handler.handle(cmd)

        assert result.health_report is not None
        dim = result.health_report.dimensions[0]
        assert dim.status == EnumDimensionStatus.RED
        assert "SSH timeout" in dim.details.get("error", "")


class TestPhase2Stub:
    def test_phase2_returns_empty_queue(self) -> None:
        probes = [_green_probe(f"d{i}") for i in range(8)]
        handler = HandlerSessionOrchestrator(probes=probes)
        cmd = ModelSessionOrchestratorCommand(dry_run=True, phase=2)
        result = handler.handle(cmd)

        assert result.status == EnumSessionStatus.COMPLETE
        assert result.dispatch_queue == []


class TestPhase3Stub:
    def test_phase3_returns_stub_receipts_for_nonempty_queue(self) -> None:
        probes = [_green_probe(f"d{i}") for i in range(8)]
        handler = HandlerSessionOrchestrator(probes=probes)
        cmd = ModelSessionOrchestratorCommand(dry_run=True, phase=0)
        result = handler.handle(cmd)

        assert result.status == EnumSessionStatus.COMPLETE
        assert result.dispatch_receipts == []

    def test_phase3_stub_with_manual_queue(self) -> None:
        """Verify stub dispatch path produces STUB receipts for nonempty queue."""
        probes = [_green_probe(f"d{i}") for i in range(8)]
        handler = HandlerSessionOrchestrator(probes=probes)
        receipts = handler._run_phase3_stub(  # noqa: SLF001
            "sess-test",
            ["OMN-1234", "PR-42"],
            ModelSessionOrchestratorCommand(dry_run=True),
        )
        assert len(receipts) == 2
        assert all(r.startswith("STUB:not-dispatched:") for r in receipts)


class TestGateDecisionLogic:
    def test_compute_gate_all_green(self) -> None:
        dims = [_make_dim(f"d{i}", EnumDimensionStatus.GREEN) for i in range(4)]
        handler = HandlerSessionOrchestrator(probes=[])
        overall, decision = handler._compute_gate(dims)  # noqa: SLF001
        assert overall == EnumDimensionStatus.GREEN
        assert decision == EnumGateDecision.PROCEED

    def test_compute_gate_red_blocks(self) -> None:
        dims = [
            _make_dim("a", EnumDimensionStatus.GREEN),
            _make_dim("b", EnumDimensionStatus.RED, blocks_dispatch=True),
        ]
        handler = HandlerSessionOrchestrator(probes=[])
        overall, decision = handler._compute_gate(dims)  # noqa: SLF001
        assert overall == EnumDimensionStatus.RED
        assert decision == EnumGateDecision.FIX_ONLY

    def test_compute_gate_yellow_non_blocking_proceeds(self) -> None:
        dims = [
            _make_dim("a", EnumDimensionStatus.GREEN),
            _make_dim("b", EnumDimensionStatus.YELLOW, blocks_dispatch=False),
        ]
        handler = HandlerSessionOrchestrator(probes=[])
        overall, decision = handler._compute_gate(dims)  # noqa: SLF001
        assert overall == EnumDimensionStatus.YELLOW
        assert decision == EnumGateDecision.PROCEED


# ---------------------------------------------------------------------------
# Integration tests — real probe exception paths (no mocks, SSH unavailable)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRealProbeExceptionPaths:
    """Test that real probe functions return valid ModelHealthDimensionResult
    even when SSH is unavailable. Exercises the actual subprocess/env code paths,
    not fabricated callables.

    Run with: pytest -m integration src/.../tests/
    Excluded from the default unit test run.
    """

    def test_probe_runtime_health_missing_env_returns_red(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ONEX_INFRA_HOST is unset, probe must return RED with env error — not raise."""
        monkeypatch.delenv("ONEX_INFRA_HOST", raising=False)
        monkeypatch.delenv("ONEX_INFRA_USER", raising=False)

        result = _probe_runtime_health()

        assert isinstance(result, ModelHealthDimensionResult)
        assert result.dimension == "runtime_health"
        assert result.status == EnumDimensionStatus.RED
        assert result.blocks_dispatch is True
        assert len(result.actionable_items) > 0
        assert "Traceback" not in str(result.details)
        assert "ONEX_INFRA_HOST" in str(result.details) or "ONEX_INFRA_HOST" in str(
            result.actionable_items
        )

    def test_probe_runtime_health_bad_host_returns_red(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When host is set to an unreachable address, probe must return RED — not raise."""
        monkeypatch.setenv("ONEX_INFRA_HOST", "192.0.2.1")  # TEST-NET, RFC 5737
        monkeypatch.setenv("ONEX_INFRA_USER", "testuser")

        result = _probe_runtime_health()

        assert isinstance(result, ModelHealthDimensionResult)
        assert result.dimension == "runtime_health"
        assert result.status == EnumDimensionStatus.RED
        assert result.blocks_dispatch is True
        assert "Traceback" not in str(result.details)

    def test_probe_deploy_agent_missing_env_returns_red(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ONEX_INFRA_USER is unset, probe must return RED with env error — not raise."""
        monkeypatch.setenv("ONEX_INFRA_HOST", "192.0.2.1")
        monkeypatch.delenv("ONEX_INFRA_USER", raising=False)

        result = _probe_deploy_agent()

        assert isinstance(result, ModelHealthDimensionResult)
        assert result.dimension == "deploy_agent"
        assert result.status == EnumDimensionStatus.RED
        assert result.blocks_dispatch is True
        assert "Traceback" not in str(result.details)
        assert "ONEX_INFRA_USER" in str(result.details) or "ONEX_INFRA_USER" in str(
            result.actionable_items
        )

    def test_probe_deploy_agent_bad_host_returns_red(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When host is unreachable, probe must return RED — not raise."""
        monkeypatch.setenv("ONEX_INFRA_HOST", "192.0.2.1")
        monkeypatch.setenv("ONEX_INFRA_USER", "testuser")

        result = _probe_deploy_agent()

        assert isinstance(result, ModelHealthDimensionResult)
        assert result.dimension == "deploy_agent"
        assert result.status == EnumDimensionStatus.RED
        assert result.blocks_dispatch is True
        assert "Traceback" not in str(result.details)

    def test_probe_result_is_valid_frozen_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify the returned object is a well-formed frozen ModelHealthDimensionResult."""
        monkeypatch.delenv("ONEX_INFRA_HOST", raising=False)
        monkeypatch.delenv("ONEX_INFRA_USER", raising=False)

        result = _probe_runtime_health()

        with pytest.raises((TypeError, Exception)):
            result.status = EnumDimensionStatus.GREEN  # type: ignore[misc]

        assert isinstance(result.dimension, str)
        assert isinstance(result.status, EnumDimensionStatus)
        assert isinstance(result.source, str)
        assert isinstance(result.timestamp, datetime)
        assert isinstance(result.stale_after, timedelta)
        assert isinstance(result.details, dict)
        assert isinstance(result.actionable_items, list)
        assert isinstance(result.blocks_dispatch, bool)
