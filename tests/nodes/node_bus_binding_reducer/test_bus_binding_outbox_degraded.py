# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A binding with a queued outbox and an unknown required topic is degraded and names the topic, never healthy.

The fixture is the STATUS row recorded at 2026-11-02T02:29:40Z: the tick printed ``state=healthy
pending_outbox=16`` while the broker answered an unknown topic or partition error for the append-completed
topic.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.bus_binding.detector import (
    decide_from_tick_evidence,
    read_tick_evidence,
)
from omnimarket.handlers.bus_binding.reducer import reduce_binding_state
from omnimarket.handlers.bus_binding.select import open_bound_bus
from omnimarket.models.bus_binding import EnumBusBinding, EnumBusBindingState
from omnimarket.nodes.node_bus_binding_reducer.handlers.handler_bus_binding_reducer import (
    HandlerBusBindingReducer,
)
from omnimarket.nodes.node_bus_binding_reducer.models import ModelBusBindingRequest

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "bus_binding_recorded_rows.json"
RECORDED = json.loads(FIXTURE.read_text(encoding="utf-8"))
MISSING = RECORDED["missing_topic"]
NOW = datetime(2026, 11, 2, 2, 29, 40, tzinfo=UTC)


def test_bus_binding_outbox_degraded_recorded_status_row_is_read() -> None:
    evidence = read_tick_evidence(RECORDED["status_row"]["detail"])
    assert evidence is not None
    assert evidence.lane == "dev"
    assert evidence.state is EnumBusBindingState.HEALTHY
    assert evidence.pending_outbox == 16
    assert evidence.missing_topics == (MISSING,)


def test_bus_binding_outbox_degraded_recorded_row_does_not_stay_healthy() -> None:
    evidence = read_tick_evidence(RECORDED["status_row"]["detail"])
    assert evidence is not None
    decision = decide_from_tick_evidence(
        evidence, prior=EnumBusBindingState.HEALTHY, observed_at=NOW
    )
    assert decision.state is EnumBusBindingState.DEGRADED
    assert decision.binding is EnumBusBinding.INMEMORY
    assert decision.pending_outbox == 16
    assert MISSING in decision.reason
    assert MISSING in decision.tick_line()
    assert "state=healthy" not in decision.tick_line()


def test_bus_binding_outbox_degraded_reducer_needs_both_facts() -> None:
    topics = (MISSING,)
    state, binding = reduce_binding_state(
        EnumBusBindingState.HEALTHY,
        durable_healthy=True,
        pending_outbox=16,
        missing_topics=topics,
    )
    assert (state, binding) == (EnumBusBindingState.DEGRADED, EnumBusBinding.INMEMORY)
    # Positive controls: either fact alone leaves the binding as the health read found it.
    assert reduce_binding_state(
        EnumBusBindingState.HEALTHY,
        durable_healthy=True,
        pending_outbox=0,
        missing_topics=topics,
    ) == (EnumBusBindingState.HEALTHY, EnumBusBinding.DURABLE)
    assert reduce_binding_state(
        EnumBusBindingState.HEALTHY,
        durable_healthy=True,
        pending_outbox=16,
        missing_topics=(),
    ) == (EnumBusBindingState.HEALTHY, EnumBusBinding.DURABLE)


def test_bus_binding_outbox_degraded_node_handler_degrades() -> None:
    result = HandlerBusBindingReducer().handle(
        ModelBusBindingRequest(
            prior_state=EnumBusBindingState.HEALTHY,
            durable_healthy=True,
            pending_outbox=16,
            missing_topics=(MISSING,),
        )
    )
    assert result.state is EnumBusBindingState.DEGRADED
    assert result.binding is EnumBusBinding.INMEMORY
    assert result.alarm is True


class _Up:
    """A durable bus that opens fine; the in-memory bus the fallback opens is a second instance."""

    def __call__(
        self, bus: str, lane: str | None, bootstrap: str | None, root: Path | None
    ) -> contextlib.AbstractAsyncContextManager[EventBusInmemory]:
        del lane, bootstrap, root
        return self._open(bus)

    @contextlib.asynccontextmanager
    async def _open(self, bus: str) -> AsyncIterator[EventBusInmemory]:
        memory = EventBusInmemory(environment="local", group=f"test-{bus}")
        await memory.start()
        try:
            yield memory
        finally:
            await memory.close()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import yaml

    declaration = tmp_path / "config" / "lab" / "ci_bus_lanes.yaml"
    declaration.parent.mkdir(parents=True)
    declaration.write_text(
        yaml.safe_dump(
            {
                "default": "inmemory",
                "lanes": {
                    "dev": {
                        "broker": "127.0.0.1:1",
                        "security_protocol": "PLAINTEXT",
                        "binding_fallback": ["inmemory"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(tmp_path))
    return tmp_path


async def _bind(
    workspace: Path, state_dir: Path, *, pending: int, missing: tuple[str, ...]
) -> tuple[EnumBusBindingState, str, list[str]]:
    lines: list[str] = []

    async def probe(_bus: object) -> tuple[str, ...]:
        return missing

    async with open_bound_bus(
        bus="kafka",
        lane="dev",
        kafka_bootstrap=None,
        workspace_root=workspace,
        state_dir=state_dir,
        open_bus=_Up(),
        pending_outbox=lambda: pending,
        missing_topics=probe,
        announce=lines.append,
        now=lambda: NOW,
    ) as bound:
        return bound.decision.state, bound.decision.reason, lines


async def test_bus_binding_outbox_degraded_open_bound_bus_falls_back(
    workspace: Path, tmp_path: Path
) -> None:
    state, reason, lines = await _bind(
        workspace, tmp_path / "state", pending=16, missing=(MISSING,)
    )
    assert state is EnumBusBindingState.DEGRADED
    assert MISSING in reason
    assert any(MISSING in line and "state=degraded" in line for line in lines)
    assert not any("state=healthy" in line for line in lines)


async def test_bus_binding_outbox_degraded_open_bound_bus_healthy_when_nothing_queued(
    workspace: Path, tmp_path: Path
) -> None:
    state, reason, _lines = await _bind(
        workspace, tmp_path / "state", pending=0, missing=(MISSING,)
    )
    assert state is EnumBusBindingState.HEALTHY
    assert reason == ""
