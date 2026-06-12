# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_volume_config_drift_sweep (OMN-12958).

Tests the handler logic with on-disk fixture files and an in-memory event bus.
No real runtime container required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_volume_config_drift_sweep.handlers.handler_volume_config_drift_sweep import (
    STATUS_DRIFTED,
    STATUS_IN_SYNC,
    NodeVolumeConfigDriftSweep,
    VolumeConfigDriftSweepRequest,
)

CMD_TOPIC = "onex.cmd.omnimarket.volume-config-drift-sweep-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.volume-config-drift-sweep-completed.v1"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.unit
class TestVolumeConfigDriftSweepGoldenChain:
    """Golden chain: command -> handler -> completion event."""

    async def test_in_sync_clean(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        deployed = _write(tmp_path / "deployed.yaml", "backends: []\n")
        source = _write(tmp_path / "source.yaml", "backends: []\n")
        result = NodeVolumeConfigDriftSweep().handle(
            VolumeConfigDriftSweepRequest(
                deployed_path=str(deployed), source_path=str(source)
            )
        )
        assert result.status == "clean"
        assert result.findings[0].status == STATUS_IN_SYNC

    async def test_drift_found(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        deployed = _write(tmp_path / "deployed.yaml", "backends: [stale]\n")
        source = _write(tmp_path / "source.yaml", "backends: []\n")
        result = NodeVolumeConfigDriftSweep().handle(
            VolumeConfigDriftSweepRequest(
                deployed_path=str(deployed),
                source_path=str(source),
                lane="stability",
            )
        )
        assert result.status == "drift_found"
        assert result.drift_count == 1
        assert result.findings[0].status == STATUS_DRIFTED

    async def test_dry_run_propagates(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        deployed = _write(tmp_path / "deployed.yaml", "backends: []\n")
        source = _write(tmp_path / "source.yaml", "backends: []\n")
        result = NodeVolumeConfigDriftSweep().handle(
            VolumeConfigDriftSweepRequest(
                deployed_path=str(deployed),
                source_path=str(source),
                dry_run=True,
            )
        )
        assert result.dry_run is True

    async def test_event_bus_wiring(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """Handler result publishes a completion event to EventBusInmemory."""
        deployed = _write(tmp_path / "deployed.yaml", "backends: [stale]\n")
        source = _write(tmp_path / "source.yaml", "backends: []\n")
        handler = NodeVolumeConfigDriftSweep()
        events_captured: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            request = VolumeConfigDriftSweepRequest(
                deployed_path=payload["deployed_path"],
                source_path=payload["source_path"],
                lane=payload.get("lane", "local"),
            )
            result = handler.handle(request)
            evt = {"status": result.status, "drift_count": result.drift_count}
            events_captured.append(evt)
            await event_bus.publish(EVT_TOPIC, key=None, value=json.dumps(evt).encode())

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-drift-sweep"
        )

        cmd_payload = json.dumps(
            {"deployed_path": str(deployed), "source_path": str(source)}
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(events_captured) == 1
        assert events_captured[0]["status"] == "drift_found"
        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()
