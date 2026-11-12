# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_floor_emissions_publish_effect --home <registry home>``.

Exit 0 when the projection was published; 1 when it was not (a topic cannot be read, or the bus refused); 65 when the overlay cannot be read.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.handlers.floor_projection import (
    BusSnapshotSource,
    InMemorySnapshotSource,
    ProtocolSnapshotSource,
    publish_envelope,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.models.floor_projection import (
    ModelFloorEmissionsOverlay,
    ModelFloorEmissionsPublishResult,
    ModelFloorProjectionEnvelope,
)
from omnimarket.nodes.node_floor_emissions_publish_effect.handlers import (
    HandlerFloorEmissionsPublish,
    InMemoryEnvelopePublisher,
    ProtocolEnvelopePublisher,
)

NODE = "node_floor_emissions_publish_effect"
EXIT_NOT_PUBLISHED = 1
EXIT_UNREADABLE = 65


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return dict(yaml.safe_load(text))


def _open_bus(overlay: ModelFloorEmissionsOverlay, env: Mapping[str, str]) -> Any:
    bootstrap = overlay.kafka_bootstrap
    return open_lane_bus(
        bus="kafka",
        lane=None if bootstrap else overlay.bus_lane,
        kafka_bootstrap=bootstrap,
        root=workspace_root(env),
    )


class BusEnvelopePublisher:
    """The real backend: one publication on the overlay's snapshot topic."""

    def __init__(self, open_bus: Callable[[], Any], topic: str) -> None:
        self._open_bus = open_bus
        self._topic = topic

    async def publish(self, envelope: ModelFloorProjectionEnvelope) -> None:
        async with self._open_bus() as bus:
            await publish_envelope(bus, self._topic, envelope)


def build_source(
    overlay: ModelFloorEmissionsOverlay,
    env: Mapping[str, str],
    group_prefix: str,
) -> ProtocolSnapshotSource:
    """The source the overlay selects: held in memory, or the broker."""
    if overlay.bus == "inmemory":
        return InMemorySnapshotSource()
    return BusSnapshotSource(
        open_bus=lambda: _open_bus(overlay, env),
        group_prefix=group_prefix,
        window_s=overlay.read_window_s,
    )


def build_publisher(
    overlay: ModelFloorEmissionsOverlay, env: Mapping[str, str]
) -> ProtocolEnvelopePublisher:
    """The publisher the overlay selects: held in memory, or the broker."""
    if overlay.bus == "inmemory":
        return InMemoryEnvelopePublisher()
    return BusEnvelopePublisher(lambda: _open_bus(overlay, env), overlay.snapshot_topic)


async def run(
    overlay: ModelFloorEmissionsOverlay,
    source: ProtocolSnapshotSource,
    publisher: ProtocolEnvelopePublisher,
    sink: ProtocolEmissionSink,
    *,
    producer: str,
    clock: Callable[[], datetime] | None = None,
) -> ModelFloorEmissionsPublishResult:
    """One pass with its chain: a start, then a success with the events published, or a typed failure."""
    contract = _contract()["runtime"]
    chain = ChainEmitter(
        sink,
        node=contract["chain"],
        piece=int(contract["piece"]),
        clock=clock,
        origin="probe",
    )
    chain.start()
    handler = HandlerFloorEmissionsPublish(
        source=source,
        publisher=publisher,
        overlay=overlay,
        producer=producer,
        clock=clock,
    )
    try:
        result = await handler.handle()
    except Exception as exc:
        chain.failure("floor_emissions_publish_failed", type(exc).__name__)
        raise
    if result.published:
        chain.success({"emissions_published": result.events})
    else:
        chain.failure("emissions_unreadable", result.reason)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE, description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    args = parser.parse_args(argv)
    env = os.environ
    contract = _contract()["runtime"]
    try:
        overlay = load_structured_overlay(
            contract["overlay"]["overlay_id"],
            ModelFloorEmissionsOverlay,
            env,
            file_env=contract["overlay"]["file_env"],
        )
    except NodeOverlayError as exc:
        sys.stderr.write(f"{NODE}: {exc}\n")
        return EXIT_UNREADABLE
    host = env.get("HOSTNAME", "") or os.uname().nodename
    try:
        result = asyncio.run(
            run(
                overlay,
                build_source(overlay, env, contract["group_prefix"]),
                build_publisher(overlay, env),
                JsonlEmissionSink(args.home / contract["journal_relpath"]),
                producer=f"{NODE}:{host}",
                clock=lambda: datetime.now(UTC),
            )
        )
    except Exception as exc:
        sys.stderr.write(f"{NODE}: {type(exc).__name__}: {exc}\n")
        return EXIT_NOT_PUBLISHED
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0 if result.published else EXIT_NOT_PUBLISHED


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["BusEnvelopePublisher", "build_publisher", "build_source", "main", "run"]
