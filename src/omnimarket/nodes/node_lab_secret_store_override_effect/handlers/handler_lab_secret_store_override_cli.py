"""Publish a local render command through the canonical ONEX dispatch runtime."""

from __future__ import annotations

import asyncio
import importlib
import sys
from importlib import resources
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.runtime.runtime_dispatch import DispatchRoute, RuntimeDispatch
from omnibase_core.runtime.transport import InMemoryBroker, InMemoryTransport

from ..models.model_lab_secret_store_override_request import (
    ModelLabSecretStoreOverrideRequest,
)
from ..models.model_lab_secret_store_override_result import (
    ModelLabSecretStoreOverrideResult,
)


async def dispatch(
    request: ModelLabSecretStoreOverrideRequest,
) -> ModelLabSecretStoreOverrideResult:
    """Run on the caller's host: its render path exists only on this filesystem.

    Reuse RuntimeDispatch for coercion, invocation and terminal publication;
    this composition root only binds the contract to its injected transport.
    """
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_lab_secret_store_override_effect")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    command_topic = contract["runtime_dispatch"]["command_topic"]
    terminal_topic = contract["runtime_dispatch"]["terminal_events"]["success"]
    route_spec = contract["handler_routing"]["handlers"][0]
    published = {
        event["event_type"]: event["topic"] for event in contract["published_events"]
    }
    if terminal_topic not in published.values():
        raise ValueError("Terminal topic is not a declared publication")
    handler_spec = route_spec["handler"]
    handler = getattr(
        importlib.import_module(handler_spec["module"]), handler_spec["name"]
    )()
    broker = InMemoryBroker()
    consumer = InMemoryTransport(
        broker=broker, group=contract["name"], topics=[command_topic]
    )
    producer = InMemoryTransport(broker=broker, group="lab-render-publisher")
    reader = InMemoryTransport(
        broker=broker, group="lab-render-terminal", topics=[terminal_topic]
    )
    correlation_id = uuid4()
    runtime = RuntimeDispatch(
        consumer=cast(Any, consumer),
        producer=producer,
        routing_map={
            command_topic: DispatchRoute(
                name=contract["name"],
                handler=handler,
                published_events=published,
                input_model_cls=ModelLabSecretStoreOverrideRequest,
            )
        },
        max_retries=0,
    )
    envelope = ModelEventEnvelope[ModelLabSecretStoreOverrideRequest](
        payload=request, correlation_id=correlation_id, event_type=command_topic
    )
    try:
        await producer.send(
            command_topic,
            key=None,
            value=envelope.model_dump_json().encode(),
            headers={},
        )
        await runtime.drain()
        await reader.start()
        messages = await reader.poll(max_messages=2, timeout_ms=0)
        if len(messages) != 1:
            raise RuntimeError(
                "Lab render command produced no unique terminal; inspect runtime errors"
            )
        terminal = ModelEventEnvelope[dict[str, Any]].model_validate_json(
            messages[0].value
        )
        if terminal.correlation_id != correlation_id:
            raise RuntimeError("Lab render terminal has a different correlation id")
        return ModelLabSecretStoreOverrideResult.model_validate(terminal.payload)
    finally:
        await reader.close()
        await consumer.close()
        await producer.close()


def main(argv: list[str] | None = None) -> int:
    """Preserve positional arguments and exit codes while publishing over the bus."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 4:
        print(
            "usage: onex-secret-store-override <kustomization.yaml> <addr> <environment-slug> <base-configmap>",
            file=sys.stderr,
        )
        return 2
    request = ModelLabSecretStoreOverrideRequest(
        kustomization=Path(args[0]),
        addr=args[1],
        environment_slug=args[2],
        base_configmap=Path(args[3]),
    )
    result = asyncio.run(dispatch(request))
    print(result.message, file=sys.stdout if result.exit_code == 0 else sys.stderr)
    return cast(int, result.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
