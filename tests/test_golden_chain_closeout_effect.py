"""Golden chain tests for node_closeout_effect.

Verifies the effect handler with protocol-based DI: mock sweeper and gate checker
are injected, dry-run bypasses side effects, failures propagate as warnings.

Related:
    - OMN-7580: Migrate node_closeout_effect to omnimarket
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_closeout_effect.handlers.handler_closeout import (
    HandlerCloseout,
)
from omnimarket.nodes.node_closeout_effect.models.model_closeout_input import (
    ModelCloseoutInput,
)
from omnimarket.nodes.node_closeout_effect.models.model_closeout_result import (
    ModelCloseoutResult,
)
from omnimarket.nodes.node_closeout_effect.protocols import (
    ProtocolMergeSweeper,
    ProtocolQualityGateChecker,
)

CMD_TOPIC = "onex.cmd.omnimarket.closeout-effect-start.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.closeout-effect-completed.v1"


class MockMergeSweeper:
    """Mock merge sweeper that returns a configurable PR count."""

    def __init__(self, prs_merged: int = 3, should_fail: bool = False) -> None:
        self._prs_merged = prs_merged
        self._should_fail = should_fail
        self.called = False

    async def sweep(self, dry_run: bool = False) -> int:
        self.called = True
        if self._should_fail:
            msg = "Mock merge sweep failure"
            raise RuntimeError(msg)
        return 0 if dry_run else self._prs_merged


class MockQualityGateChecker:
    """Mock quality gate checker that returns a configurable result."""

    def __init__(self, passes: bool = True, should_fail: bool = False) -> None:
        self._passes = passes
        self._should_fail = should_fail
        self.called = False

    async def check(self, dry_run: bool = False) -> bool:
        self.called = True
        if self._should_fail:
            msg = "Mock quality gate failure"
            raise RuntimeError(msg)
        return True if dry_run else self._passes


def _make_input(dry_run: bool = False) -> ModelCloseoutInput:
    return ModelCloseoutInput(correlation_id=uuid4(), dry_run=dry_run)


@pytest.mark.unit
class TestCloseoutEffectGoldenChain:
    """Golden chain: closeout effect with protocol-based DI."""

    async def test_dry_run_skips_side_effects(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Dry run returns synthetic success without calling protocols."""
        sweeper = MockMergeSweeper()
        checker = MockQualityGateChecker()
        handler = HandlerCloseout(merge_sweeper=sweeper, quality_gate_checker=checker)
        inp = _make_input(dry_run=True)

        result = await handler.handle(
            correlation_id=inp.correlation_id, dry_run=inp.dry_run
        )

        assert result.merge_sweep_completed is True
        assert result.prs_merged == 0
        assert result.quality_gates_passed is True
        assert result.release_ready is True
        assert "dry_run" in result.warnings[0]
        # Protocols should NOT have been called
        assert sweeper.called is False
        assert checker.called is False

    async def test_all_pass_with_injected_protocols(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Both protocols succeed -> release_ready=True."""
        sweeper = MockMergeSweeper(prs_merged=5)
        checker = MockQualityGateChecker(passes=True)
        handler = HandlerCloseout(merge_sweeper=sweeper, quality_gate_checker=checker)
        inp = _make_input()

        result = await handler.handle(
            correlation_id=inp.correlation_id, dry_run=inp.dry_run
        )

        assert result.merge_sweep_completed is True
        assert result.prs_merged == 5
        assert result.quality_gates_passed is True
        assert result.release_ready is True
        assert len(result.warnings) == 0
        assert sweeper.called is True
        assert checker.called is True

    async def test_merge_sweep_failure_degrades_gracefully(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Merge sweep failure -> release_ready=False, warning captured."""
        sweeper = MockMergeSweeper(should_fail=True)
        checker = MockQualityGateChecker(passes=True)
        handler = HandlerCloseout(merge_sweeper=sweeper, quality_gate_checker=checker)
        inp = _make_input()

        result = await handler.handle(
            correlation_id=inp.correlation_id, dry_run=inp.dry_run
        )

        assert result.merge_sweep_completed is False
        assert result.release_ready is False
        assert any("Merge sweep warning" in w for w in result.warnings)

    async def test_quality_gate_failure_degrades_gracefully(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Quality gate failure -> release_ready=False, warning captured."""
        sweeper = MockMergeSweeper(prs_merged=2)
        checker = MockQualityGateChecker(should_fail=True)
        handler = HandlerCloseout(merge_sweeper=sweeper, quality_gate_checker=checker)
        inp = _make_input()

        result = await handler.handle(
            correlation_id=inp.correlation_id, dry_run=inp.dry_run
        )

        assert result.merge_sweep_completed is True
        assert result.quality_gates_passed is False
        assert result.release_ready is False
        assert any("Quality gates warning" in w for w in result.warnings)

    async def test_quality_gate_returns_false(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Quality gate returns False (no exception) -> release not ready."""
        sweeper = MockMergeSweeper(prs_merged=1)
        checker = MockQualityGateChecker(passes=False)
        handler = HandlerCloseout(merge_sweeper=sweeper, quality_gate_checker=checker)
        inp = _make_input()

        result = await handler.handle(
            correlation_id=inp.correlation_id, dry_run=inp.dry_run
        )

        assert result.quality_gates_passed is False
        assert result.release_ready is False
        assert len(result.warnings) == 0  # No exception, no warning

    async def test_no_protocols_injected(self, event_bus: EventBusInmemory) -> None:
        """Handler works with no protocols injected (graceful degradation)."""
        handler = HandlerCloseout()
        inp = _make_input()

        result = await handler.handle(
            correlation_id=inp.correlation_id, dry_run=inp.dry_run
        )

        assert result.merge_sweep_completed is True
        assert result.prs_merged == 0
        assert result.quality_gates_passed is True
        assert result.release_ready is True

    async def test_result_model_frozen(self, event_bus: EventBusInmemory) -> None:
        """Result model is frozen (immutable)."""
        handler = HandlerCloseout()
        inp = _make_input(dry_run=True)

        result = await handler.handle(
            correlation_id=inp.correlation_id, dry_run=inp.dry_run
        )

        with pytest.raises(Exception, match="frozen"):
            result.prs_merged = 99  # type: ignore[misc]

    async def test_input_model_frozen(self, event_bus: EventBusInmemory) -> None:
        """Input model is frozen (immutable)."""
        inp = _make_input()

        with pytest.raises(Exception, match="frozen"):
            inp.dry_run = True  # type: ignore[misc]

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler result can be published to EventBusInmemory."""
        handler = HandlerCloseout()
        completed_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            result = await handler.handle(
                correlation_id=payload["correlation_id"],
                dry_run=payload.get("dry_run", False),
            )
            result_payload = result.model_dump(mode="json")
            completed_events.append(result_payload)
            await event_bus.publish(
                COMPLETED_TOPIC,
                key=None,
                value=json.dumps(result_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-closeout-effect"
        )

        cmd_payload = json.dumps(
            {"correlation_id": str(uuid4()), "dry_run": True}
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["release_ready"] is True

        history = await event_bus.get_event_history(topic=COMPLETED_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_protocol_structural_subtyping(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Mock classes satisfy protocol structural subtyping."""
        sweeper: ProtocolMergeSweeper = MockMergeSweeper()
        checker: ProtocolQualityGateChecker = MockQualityGateChecker()

        # If this assignment works without type errors, protocols are satisfied
        handler = HandlerCloseout(merge_sweeper=sweeper, quality_gate_checker=checker)
        result = await handler.handle(correlation_id=uuid4())

        assert isinstance(result, ModelCloseoutResult)
