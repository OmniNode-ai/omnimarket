# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``host-install``: publish a host install request on the real bus and wait for
its terminal (OMN-23957).

    onex host-install request --host h202 --unit omni --actor ci --platform systemd \
        --label omni --registry-request-file registry.json --timeout-s 300 \
        --bus kafka --bus-lane lab

Exit codes: 0 installed, 1 rolled_back or failed, 65 the request file or options
refused nothing published, 69 the bus is unavailable, 124 the terminal did not
arrive in time.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import json
import uuid
from pathlib import Path
from typing import Any

import click
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolLabRunBus,
    _envelope_bytes,
    event_type_for,
)
from omnimarket.delegated_test_loop.lane_bus import (
    BusKind,
    LabRunBusError,
    open_lab_run_bus,
)
from omnimarket.handlers.bus_binding.select import positional_opener
from omnimarket.lab_work.cli import _with_bus_options
from omnimarket.nodes.node_host_install_orchestrator.models import (
    ModelHostInstallCompleted,
    ModelHostInstallRequest,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)

EXIT_VALIDATION = 65
EXIT_BUS_UNAVAILABLE = 69
EXIT_TIMEOUT = 124


def _contract_topics() -> tuple[str, str]:
    """Read the command and terminal topics from the node's contract.yaml."""
    contract = importlib.resources.files(
        "omnimarket.nodes.node_host_install_orchestrator"
    ).joinpath("contract.yaml")
    document = yaml.safe_load(contract.read_text())
    command_topic = document["runtime_dispatch"]["command_topic"]
    terminal_event = document["terminal_event"]
    return command_topic, terminal_event


async def request_install(
    bus: ProtocolLabRunBus, request: ModelHostInstallRequest, timeout_s: float
) -> ModelHostInstallCompleted:
    """Subscribe to the terminal topic, publish the request, and return the matching terminal."""
    from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
        require_ungrouped,
    )

    command_topic, terminal_topic = _contract_topics()
    ungrouped = require_ungrouped(bus)
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    correlation = str(request.correlation_id)

    async def on_message(message: Any) -> None:
        try:
            raw = json.loads(message.value)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            return
        if payload.get("correlation_id") != correlation:
            return
        if not future.done():
            future.set_result(raw)

    unsubscribe = await ungrouped.subscribe_ungrouped(terminal_topic, on_message)
    try:
        envelope = ModelEventEnvelope[dict[str, Any]](
            payload=request.model_dump(mode="json"),
            correlation_id=uuid.UUID(correlation),
            event_type=event_type_for(command_topic),
        )
        await bus.publish(
            command_topic,
            correlation.encode("utf-8"),
            _envelope_bytes(envelope),
        )
        raw = await asyncio.wait_for(future, timeout=timeout_s)
    finally:
        await unsubscribe()
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise TimeoutError("the terminal carried no payload")
    return ModelHostInstallCompleted.model_validate(payload)


@click.group("host-install")
@click.pass_context
def host_install_group(ctx: click.Context) -> None:
    """Publish a host install request on the bus and wait for its terminal."""
    ctx.ensure_object(dict)


@host_install_group.command("request")
@_with_bus_options
@click.option("--host", required=True, help="The host the install targets.")
@click.option("--unit", required=True, help="The unit being installed.")
@click.option("--actor", required=True, help="The actor requesting the install.")
@click.option(
    "--platform",
    type=click.Choice(["systemd", "launchd"]),
    required=True,
    help="The host's service platform.",
)
@click.option("--label", required=True, help="The install label.")
@click.option(
    "--registry-request-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to a ModelRegistryUnitsRequest JSON document.",
)
@click.option(
    "--timeout-s",
    type=float,
    required=True,
    help="Seconds to wait for the terminal.",
)
def request_command(
    omnibase_path: Path | None,
    bus: BusKind,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
    host: str,
    unit: str,
    actor: str,
    platform: str,
    label: str,
    registry_request_file: Path,
    timeout_s: float,
) -> None:
    """Publish a host install request and print the terminal."""
    try:
        registry = ModelRegistryUnitsRequest.model_validate_json(
            registry_request_file.read_text()
        )
    except Exception as exc:
        click.echo(f"registry request file: {exc}", err=True)
        raise SystemExit(EXIT_VALIDATION) from exc

    correlation_id = uuid.uuid4()
    request = ModelHostInstallRequest(
        correlation_id=correlation_id,
        host=host,
        unit=unit,
        actor=actor,
        platform=platform,
        label=label,
        registry_request=registry,
    )

    async def main() -> int:
        try:
            async with positional_opener(open_lab_run_bus)(
                bus, bus_lane, kafka_bootstrap, omnibase_path
            ) as opened:
                completed = await request_install(opened, request, timeout_s)
        except LabRunBusError as exc:
            click.echo(f"bus: {exc}", err=True)
            return EXIT_BUS_UNAVAILABLE
        except TimeoutError:
            click.echo("no terminal arrived in time", err=True)
            return EXIT_TIMEOUT
        click.echo(completed.model_dump_json())
        if completed.outcome == "installed":
            return 0
        return 1

    code = asyncio.run(main())
    if code != 0:
        raise SystemExit(code)


if __name__ == "__main__":
    host_install_group()
