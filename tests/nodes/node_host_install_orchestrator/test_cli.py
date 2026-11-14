# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command-line path of the host install: one request envelope published on the command topic, the matching
terminal read back (OMN-23957).

A stand-in runtime answers the command topic with the real chain (``Plant``: orchestrator, file effect, readback and
reducer handlers over fakes) and publishes the terminal as the runtime would, wrapped in an envelope. The CLI itself runs
against that bus through ``open_lab_run_bus``, so the exit codes are read from the command, not from a helper.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import _envelope_bytes, event_type_for
from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.container.container_inmemory import bind_infra_event_bus
from omnimarket.nodes.node_host_install_orchestrator import cli
from omnimarket.nodes.node_host_install_orchestrator.cli import (
    host_install_group,
    request_install,
)
from omnimarket.nodes.node_host_install_orchestrator.models import (
    ModelHostInstallCompleted,
    ModelHostInstallRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import require_ungrouped
from tests.nodes.node_host_install_orchestrator.support import Plant, request
from tests.nodes.node_host_install_orchestrator.test_seam import (
    MISMATCHED_TIMER,
    fresh,
)

pytestmark = pytest.mark.unit

TERMINAL_TIMEOUT_S = 2.0


class ServedBus:
    """An in-memory bus with a stand-in runtime on the command topic."""

    def __init__(self, *, answer_as: uuid.UUID | None, plant: Plant | None) -> None:
        self.bus = bind_infra_event_bus(environment="local", group="host-install-cli")
        self.plant = plant
        self.answer_as = answer_as
        self.commands: list[ModelHostInstallRequest] = []

    async def start(self) -> None:
        await self.bus.start()
        command_topic, _ = cli._contract_topics()
        await require_ungrouped(self.bus).subscribe_ungrouped(
            command_topic, self._on_command
        )

    async def _on_command(self, message: Any) -> None:
        envelope = json.loads(message.value)
        start = ModelHostInstallRequest.model_validate(envelope["payload"])
        self.commands.append(start)
        if self.plant is None:
            return
        self.plant.run(start)
        assert self.plant.completed is not None
        completed = self.plant.completed.model_copy(
            update={"correlation_id": self.answer_as or start.correlation_id}
        )
        _, terminal_topic = cli._contract_topics()
        terminal = ModelEventEnvelope[dict[str, Any]](
            payload=completed.model_dump(mode="json"),
            correlation_id=completed.correlation_id,
            event_type=event_type_for(terminal_topic),
        )
        await self.bus.publish(
            terminal_topic,
            str(completed.correlation_id).encode(),
            _envelope_bytes(terminal),
        )


def _registry_file(directory: Path) -> Path:
    path = directory / "registry.json"
    path.write_text(request().registry_request.model_dump_json(), encoding="utf-8")
    return path


def _args(registry_file: Path) -> list[str]:
    start = request()
    return [
        "request",
        "--host",
        start.host,
        "--unit",
        start.unit,
        "--actor",
        start.actor,
        "--platform",
        start.platform,
        "--label",
        start.label,
        "--registry-request-file",
        str(registry_file),
        "--timeout-s",
        str(TERMINAL_TIMEOUT_S),
        "--bus",
        "inmemory",
    ]


def _run_cli(
    monkeypatch: pytest.MonkeyPatch, served: ServedBus, registry_file: Path
) -> Any:
    @contextlib.asynccontextmanager
    async def open_served(**_: Any) -> AsyncIterator[Any]:
        await served.start()
        try:
            yield served.bus
        finally:
            await served.bus.close()

    monkeypatch.setattr(cli, "open_lab_run_bus", open_served)
    return CliRunner().invoke(host_install_group, _args(registry_file))


def test_golden_the_command_line_publishes_the_request_and_prints_the_installed_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    served = ServedBus(answer_as=None, plant=fresh())
    result = _run_cli(monkeypatch, served, _registry_file(tmp_path))
    assert result.exit_code == 0, result.output
    completed = ModelHostInstallCompleted.model_validate_json(result.output)
    assert completed.outcome == "installed"
    assert [c.host for c in served.commands] == [request().host]


def test_golden_a_rolled_back_install_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    served = ServedBus(answer_as=None, plant=fresh(install_timer=MISMATCHED_TIMER))
    result = _run_cli(monkeypatch, served, _registry_file(tmp_path))
    assert result.exit_code == 1, result.output
    assert (
        ModelHostInstallCompleted.model_validate_json(result.output).outcome
        == "rolled_back"
    )


def test_error_chain_no_terminal_exits_124(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    served = ServedBus(answer_as=None, plant=None)
    result = _run_cli(monkeypatch, served, _registry_file(tmp_path))
    assert result.exit_code == 124
    assert len(served.commands) == 1


def test_error_chain_an_unreadable_registry_file_exits_65_and_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    served = ServedBus(answer_as=None, plant=fresh())
    result = _run_cli(monkeypatch, served, bad)
    assert result.exit_code == 65
    assert served.commands == []


def test_error_chain_an_unavailable_bus_exits_69(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    @contextlib.asynccontextmanager
    async def refused(**_: Any) -> AsyncIterator[Any]:
        raise LabRunBusError("no broker")
        yield

    monkeypatch.setattr(cli, "open_lab_run_bus", refused)
    result = CliRunner().invoke(host_install_group, _args(_registry_file(tmp_path)))
    assert result.exit_code == 69


def test_failing_control_a_terminal_for_another_install_is_not_returned(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        served = ServedBus(answer_as=uuid.uuid4(), plant=fresh())
        await served.start()
        try:
            with pytest.raises(TimeoutError):
                await request_install(served.bus, request(), TERMINAL_TIMEOUT_S)
        finally:
            await served.bus.close()

    asyncio.run(run())


def test_the_command_line_names_no_topic_and_no_default_wait() -> None:
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "onex.cmd." not in source
    assert "onex.evt." not in source
    timeout = next(
        p
        for p in host_install_group.commands["request"].params
        if p.name == "timeout_s"
    )
    assert timeout.required is True
