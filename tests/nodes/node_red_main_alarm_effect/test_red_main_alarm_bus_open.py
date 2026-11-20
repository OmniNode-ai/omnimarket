# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm node's bus open inside a shared runtime whose event loop stalls.

The recorded cause (fixtures/degraded_binding_reading.json): on the dev runtime the node's fires
answered ``configuration_unreadable: alarm bus binding is degraded`` for hours, while a fresh process in
the same container opened the same lane in well under a second. The binding was degraded by the client
command's 8 s health read (reason ``TimeoutError``), which the runtime's own loop (a single process held
near one core, log gaps of 3 to 7 s, aiokafka heartbeat sessions expiring) can miss while the broker is
healthy. The message then dropped the reason, so each earlier fix reached only the next symptom.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.bus_binding import select as select_module
from omnimarket.nodes.node_red_main_alarm_effect import ports as ports_module
from omnimarket.nodes.node_red_main_alarm_effect.models import (
    ModelAlarmRepository,
    ModelAlarmSettings,
)
from omnimarket.nodes.node_red_main_alarm_effect.ports import AlarmBusPorts

FIXTURE = Path(__file__).parent / "fixtures" / "degraded_binding_reading.json"
HEALTH_READ_BOUND_OF_A_CLIENT_COMMAND = 30.0


def _settings(root: Path, **extra: Any) -> ModelAlarmSettings:
    return ModelAlarmSettings(
        parent_lane="fixture-parent",
        host="fixture-host",
        workspace_config_root=str(root),
        repositories=(
            ModelAlarmRepository(
                repo="OmniNode-ai/overlays",
                branch="main",
                owner="fixture-owner",
                required_checks=("ci",),
            ),
        ),
        **extra,
    )


class _Writer:
    def __init__(self, bus: object, *, principal: str) -> None:
        self.bus = bus

    async def stop(self) -> None:
        return None


@pytest.fixture
def lane(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A lane declaring the in-memory fallback, as the dev lane does, with quiet exception steps."""
    mounted = tmp_path / "mounted"
    (mounted / "config" / "lab").mkdir(parents=True)
    (mounted / "config" / "lab" / "ci_bus_lanes.yaml").write_text(
        "lanes:\n  dev:\n    binding_fallback: [inmemory]\n"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)
    monkeypatch.setenv("OMNIBASE_PATH", str(workspace))
    real = select_module.open_bound_bus

    def quiet(**kwargs: Any) -> Any:
        return real(
            **kwargs,
            state_dir=tmp_path / "state",
            alarm=lambda _dir, _decision: "noop",
            settle=lambda _dir, _decision: None,
        )

    monkeypatch.setattr(ports_module, "open_bound_bus", quiet)
    monkeypatch.setattr(ports_module, "WorkLedgerAppendCaller", _Writer)
    monkeypatch.setattr(ports_module, "_writer_principal", lambda: "fixture-login")
    return mounted


def _kafka_opener(monkeypatch: pytest.MonkeyPatch, behaviour: Any) -> list[str]:
    """Replace the lane bus opener: ``behaviour(call_number)`` runs inside each durable open."""
    calls: list[str] = []

    @asynccontextmanager
    async def opener(
        *,
        bus: str,
        lane: str | None,
        kafka_bootstrap: str | None,
        root: Path | None,
    ) -> Any:
        if bus == "kafka":
            calls.append(bus)
            await behaviour(len(calls))
        yield EventBusInmemory(environment="test", group="test")

    monkeypatch.setattr(ports_module, "open_lab_run_bus", opener)
    return calls


async def test_the_health_read_bound_is_sized_for_a_runtime_hosted_open(
    lane: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound the open applies is not the 8 s a short-lived client command applies."""
    seen: dict[str, float] = {}
    inner = ports_module.open_bound_bus

    def spy(**kwargs: Any) -> Any:
        default = (
            inspect.signature(select_module.open_bound_bus)
            .parameters["timeout_s"]
            .default
        )
        seen["timeout_s"] = kwargs.get("timeout_s", default)
        return inner(**kwargs)

    monkeypatch.setattr(ports_module, "open_bound_bus", spy)

    async def healthy(_call: int) -> None:
        return None

    _kafka_opener(monkeypatch, healthy)

    async with AlarmBusPorts.open(_settings(lane)):
        pass

    assert seen["timeout_s"] >= HEALTH_READ_BOUND_OF_A_CLIENT_COMMAND, (
        f"the open applies a {seen['timeout_s']} s health read"
    )


async def test_a_stalled_first_open_is_retried_instead_of_reported_degraded(
    lane: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runtime loop that stalls past the bound once must not turn the fire into a failure."""

    async def stalls_once(call: int) -> None:
        if call == 1:
            await asyncio.sleep(5)

    calls = _kafka_opener(monkeypatch, stalls_once)

    async with AlarmBusPorts.open(
        _settings(lane, bus_open_timeout_seconds=0.2, bus_open_attempts=2)
    ) as ports:
        assert isinstance(ports, AlarmBusPorts)

    assert calls == ["kafka", "kafka"]


async def test_a_binding_that_stays_degraded_names_its_reason_and_the_attempts(
    lane: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure the consumer reads says why: the decision's reason, the attempts, the bound."""

    async def refuses(_call: int) -> None:
        raise LabRunBusError("fixture broker refused the login")

    calls = _kafka_opener(monkeypatch, refuses)

    with pytest.raises(RuntimeError) as raised:
        async with AlarmBusPorts.open(
            _settings(lane, bus_open_timeout_seconds=1.0, bus_open_attempts=2)
        ):
            pass

    message = str(raised.value)
    assert message.startswith("alarm bus binding is degraded")
    assert "fixture broker refused the login" in message
    assert "attempts=2" in message
    assert len(calls) == 2


def test_the_recorded_reading_is_the_one_the_cause_was_drawn_from() -> None:
    """The fixture holds the log lines the cause was read from, not an invented shape."""
    reading = json.loads(FIXTURE.read_text())
    assert reading["bus_binding_line"].endswith("reason=TimeoutError: ")
    assert reading["fire_error_line"].endswith("detail=alarm bus binding is degraded")
    assert reading["fresh_process_open_seconds"] < 1.0
    assert reading["health_read_bound_seconds"] == 8.0
    assert reading["log_gaps_of_4s_or_more_since_0600"] > 100
