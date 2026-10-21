# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed operator ingress through the canonical core bus on the deployment host."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from omnibase_core.models.core.model_envelope_metadata import ModelEnvelopeMetadata
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models import ModelEventMessage
from omnibase_infra.runtime.auto_wiring.discovery import _parse_contract
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models.model_auto_wiring_manifest import (
    ModelAutoWiringManifest,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from pydantic import ValidationError

from ..models.model_runtime_deploy_request import (
    ModelRuntimeDeployOptions,
    ModelRuntimeDeployRequest,
)
from ..models.model_runtime_deploy_result import ModelRuntimeDeployResult
from .handler_runtime_deploy import CONTRACT_PATH, contract_topics


async def dispatch(
    request: ModelRuntimeDeployRequest,
    *,
    environment: dict[str, str] | None = None,
) -> ModelRuntimeDeployResult:
    """Publish a command, dispatch its contract route, consume its own terminal.

    This host-bound operator ingress uses the shared core transport, not direct
    handler invocation as a CLI path. The same definition-B handler and contract
    are discoverable by the runtime through onex.nodes.
    """
    command_topic, terminal_topic = contract_topics()
    contract = _parse_contract(
        contract_path=CONTRACT_PATH,
        entry_point_name="node_runtime_deploy_effect",
        package_name="omnimarket",
        package_version="0.1.0",
    )
    manifest = ModelAutoWiringManifest(
        contracts=(contract,), runtime_profile="operator"
    )
    engine = MessageDispatchEngine()
    bus = EventBusInmemory(environment="operator", group="runtime-deploy")
    terminal: asyncio.Future[ModelRuntimeDeployResult] = (
        asyncio.get_running_loop().create_future()
    )

    async def consume_terminal(message: ModelEventMessage) -> None:
        try:
            envelope = ModelEventEnvelope[ModelRuntimeDeployResult].model_validate_json(
                message.value
            )
            if (
                envelope.correlation_id == request.correlation_id
                and not terminal.done()
            ):
                terminal.set_result(envelope.payload)
        except ValueError as exc:
            if not terminal.done():
                terminal.set_exception(exc)

    await bus.start()
    try:
        await bus.subscribe(
            terminal_topic,
            group_id="runtime-deploy-caller",
            on_message=consume_terminal,
        )
        report = await wire_from_manifest(
            manifest=manifest,
            dispatch_engine=engine,
            event_bus=bus,
            environment="operator",
            materialized_explicit_dependencies={
                "HandlerRuntimeDeploy": {"environment": environment}
            },
        )
        if report.total_wired != 1 or report.total_failed:
            raise ValueError("runtime deployment did not wire exactly one contract")
        engine.freeze()
        await bus.publish_envelope(
            ModelEventEnvelope(
                event_type=command_topic,
                payload=request,
                correlation_id=request.correlation_id,
                metadata=ModelEnvelopeMetadata(tags={"message_category": "command"}),
            ),
            command_topic,
        )
        return await terminal
    finally:
        await bus.shutdown()


def main(argv: Sequence[str] | None = None) -> int:
    """Keep the host deployment's flags and streams; explicitly bind its source."""
    parser = argparse.ArgumentParser(prog="onex-runtime-deploy", add_help=False)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    for flag in ("execute", "force", "restart", "cold", "readback-only", "prod"):
        parser.add_argument(f"--{flag}", action="store_true")
    parser.add_argument("--profile")
    parser.add_argument("--lock-timeout", type=int)
    parser.add_argument("--effects-plan")
    parser.add_argument(
        "--print-compose-cmd", dest="print_compose_command", action="store_true"
    )
    parser.add_argument("--help", "-h", dest="show_help", action="store_true")
    args = vars(parser.parse_args(argv))
    repository_root = args.pop("repository_root").resolve()
    try:
        request = ModelRuntimeDeployRequest(
            correlation_id=uuid4(),
            repository_root=repository_root,
            options=ModelRuntimeDeployOptions.model_validate(args),
        )
        result = asyncio.run(dispatch(request, environment=dict(os.environ)))
    except (OSError, ValueError, ValidationError) as exc:
        sys.stderr.write(f"onex-runtime-deploy: {exc}\n")
        return 64
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
