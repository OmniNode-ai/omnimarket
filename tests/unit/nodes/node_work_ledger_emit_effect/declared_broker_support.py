# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Test support (OMN-23452): a workspace whose config root declares the lane the work-ledger emit dials.

The lane declaration and the emit node's overlay are written under ``tmp_path`` and selected the way a
deployment selects them (``ONEX_WORKSPACE_CONFIG_ROOT``). The declared broker is a loopback address
(read from ``configs/network_bindings.yaml``), so the emit guard, which refuses a test process that
would publish to a non-loopback broker, still lets the in-memory bus carry the event.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.network_bindings import network_host, network_port
from omnimarket.nodes.node_work_ledger_emit_effect.handlers import (
    handler_work_ledger_declared_broker_emit as declared,
)

_AMBIENT = (
    "KAFKA_BOOTSTRAP_SERVERS",
    "KAFKA_SECURITY_PROTOCOL",
    "KAFKA_SASL_MECHANISM",
    "KAFKA_SASL_USERNAME",
    "KAFKA_SASL_PASSWORD",
    "ONEX_EMIT_EFFECT_SPOOL_ONLY",
    "OMNI_HOME",
)


def loopback_broker() -> str:
    """The declared test broker: the loopback address with the emit daemon's port from the bindings."""
    return f"{network_host('loopback.ipv4')}:{network_port('emit_daemon.port')}"


def install_emit_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_bus: dict[str, object] | None,
) -> Path:
    """Write the lane declaration and the emit node's overlay (``event_bus`` omitted when None)."""
    for name in _AMBIENT:
        monkeypatch.delenv(name, raising=False)
    config_root = tmp_path / "config-owner"
    declaration = config_root / "config" / "lab" / "ci_bus_lanes.yaml"
    declaration.parent.mkdir(parents=True, exist_ok=True)
    declaration.write_text(
        yaml.safe_dump(
            {
                "lanes": {
                    "dev": {
                        "broker": loopback_broker(),
                        "security_protocol": "PLAINTEXT",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    overlay = config_root / "config" / "onex" / "overlays" / declared.NODE_NAME
    overlay.mkdir(parents=True, exist_ok=True)
    body: dict[str, object] = {}
    if event_bus is not None:
        body["event_bus"] = event_bus
    (overlay / "overlay.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(config_root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OMNIBASE_PATH", str(tmp_path / "workspace"))
    monkeypatch.setenv("ONEX_EMIT_EFFECT_SPOOL_DIR", str(tmp_path / "spool"))
    return config_root


class OpenInmemoryBus:
    """One in-memory bus the publisher may close after each publish without losing the history."""

    def __init__(self) -> None:
        self.bus = EventBusInmemory()
        self.dialed: list[str | None] = []

    async def start(self) -> None:
        await self.bus.start()

    async def close(self) -> None:
        return None

    async def publish(self, **kwargs: Any) -> Any:
        return await self.bus.publish(**kwargs)

    async def published(self, topic: str) -> list[dict[str, Any]]:
        history = await self.bus.get_event_history(limit=1000, topic=topic)
        return [json.loads(message.value) for message in history]


def route_declared_bus(monkeypatch: pytest.MonkeyPatch, bus: OpenInmemoryBus) -> None:
    """Swap the real Kafka bus builder for the in-memory bus, recording the broker it was asked to dial."""

    def build(binding: Any) -> OpenInmemoryBus:
        bus.dialed.append(binding.bootstrap_servers)
        return bus

    monkeypatch.setattr(declared, "declared_kafka_bus", build)
