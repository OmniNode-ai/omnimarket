# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerSessionOrchestrator (OMN-8367 PoC).

All external probes are faked — no SSH, no subprocess, no network calls.
Tests cover Phase 1 health gate logic; Phases 2 and 3 are stubs (tested for
stub behavior, not real behavior).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator import (
    EnumDimensionStatus,
    EnumGateDecision,
    EnumSessionStatus,
    HandlerSessionOrchestrator,
    ModelHealthDimensionResult,
    ModelSessionOrchestratorCommand,
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


def _green_probe(name: str) -> callable:
    def probe() -> ModelHealthDimensionResult:
        return _make_dim(name, EnumDimensionStatus.GREEN)

    probe.__name__ = f"_probe_{name}"
    return probe


def _red_probe(name: str, blocks: bool = False) -> callable:
    def probe() -> ModelHealthDimensionResult:
        return _make_dim(name, EnumDimensionStatus.RED, blocks_dispatch=blocks)

    probe.__name__ = f"_probe_{name}"
    return probe


def _yellow_probe(name: str, blocks: bool = False) -> callable:
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

        def probe():
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
        # Phase 0 = all phases; queue will be empty from stub Phase 2
        cmd = ModelSessionOrchestratorCommand(dry_run=True, phase=0)
        result = handler.handle(cmd)

        assert result.status == EnumSessionStatus.COMPLETE
        # Queue is empty (stub Phase 2), so receipts are empty too
        assert result.dispatch_receipts == []

    def test_phase3_stub_with_manual_queue(self) -> None:
        """Verify stub dispatch path produces STUB receipts for nonempty queue."""
        probes = [_green_probe(f"d{i}") for i in range(8)]
        handler = HandlerSessionOrchestrator(probes=probes)
        # Directly test the stub method
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
