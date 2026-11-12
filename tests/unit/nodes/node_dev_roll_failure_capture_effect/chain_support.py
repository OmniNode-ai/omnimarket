"""Chain support for node_dev_roll_failure_capture_effect tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_dev_roll_failure_capture_effect.adapters.adapter_in_memory import (
    InMemoryRollArtifactStore,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers.handler_dev_roll_incident_capture import (
    HandlerDevRollIncidentCapture,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers.handler_dev_roll_manifest_record import (
    HandlerDevRollManifestRecord,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.overlay import (
    NODE_NAME,
    ModelDevRollCaptureSettings,
    load_settings,
)


def discover_contracts_from_paths(paths: list[Path]) -> list[dict[str, Any]]:
    contracts = []
    for p in paths:
        if p.is_file() and p.suffix == ".yaml":
            with open(p) as f:
                contracts.append(yaml.safe_load(f))
    return contracts


def build_routing_map(
    contracts: list[dict[str, Any]],
    handler_resolver: Any,
) -> dict[str, Any]:
    routing: dict[str, Any] = {}
    for contract in contracts:
        routing_block = contract.get("handler_routing", {})
        for entry in routing_block.get("entries", []):
            topic = entry.get("topic")
            handler_name = entry.get("handler")
            if topic and handler_name:
                routing[topic] = handler_resolver(handler_name)
    return routing


class RuntimeDispatch:
    def __init__(self, routing: dict[str, Any], transport: Any):
        self._routing = routing
        self._transport = transport

    async def dispatch(self, topic: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        handler = self._routing.get(topic)
        if handler is None:
            return []
        result = await handler.handle(payload)
        return [result]


class InMemoryTransport:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, topic: str, body: Any) -> None:
        self.published.append({"topic": topic, "body": body})


def make_settings() -> ModelDevRollCaptureSettings:
    return load_settings({
        "artifact_root": "/tmp/test-artifacts",
        "retention_days": 7,
        "log_byte_cap": 1024,
    })


def make_handler_resolver(store: InMemoryRollArtifactStore) -> Any:
    settings = make_settings()

    def resolver(handler_name: str) -> Any:
        if handler_name == "HandlerDevRollIncidentCapture":
            return HandlerDevRollIncidentCapture(store=store, settings=settings)
        if handler_name == "HandlerDevRollManifestRecord":
            return HandlerDevRollManifestRecord(store=store)
        raise ValueError(f"unknown handler: {handler_name}")

    return resolver
