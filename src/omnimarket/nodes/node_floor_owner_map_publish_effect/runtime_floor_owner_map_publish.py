# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_floor_owner_map_publish_effect --home <registry home>``.

Exit 0 when the map was published; 1 when it was not (the state cannot be read, or the bus refused); 65 when the overlay cannot be read.
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
    publish_envelope,
    read_controller_owners,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.models.floor_projection import (
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionPublishOverlay,
    ModelFloorProjectionPublishRequest,
    ModelFloorProjectionPublishResult,
)
from omnimarket.nodes.node_floor_owner_map_publish_effect.handlers import (
    HandlerFloorOwnerMapPublish,
    InMemoryEnvelopePublisher,
    ProtocolEnvelopePublisher,
)

NODE = "node_floor_owner_map_publish_effect"
EXIT_NOT_PUBLISHED = 1
EXIT_UNREADABLE = 65


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return dict(yaml.safe_load(text))


class BusEnvelopePublisher:
    """The real backend: one publication on the contract's topic."""

    def __init__(self, open_bus: Callable[[], Any], topic: str) -> None:
        self._open_bus = open_bus
        self._topic = topic

    async def publish(self, envelope: ModelFloorProjectionEnvelope) -> None:
        async with self._open_bus() as bus:
            await publish_envelope(bus, self._topic, envelope)


def build_publisher(
    overlay: ModelFloorProjectionPublishOverlay,
    env: Mapping[str, str],
    topic: str,
) -> ProtocolEnvelopePublisher:
    """The publisher the overlay selects: held in memory, or the broker."""
    if overlay.bus == "inmemory":
        return InMemoryEnvelopePublisher()
    bootstrap = overlay.kafka_bootstrap
    return BusEnvelopePublisher(
        lambda: open_lane_bus(
            bus="kafka",
            lane=None if bootstrap else overlay.bus_lane,
            kafka_bootstrap=bootstrap,
            root=workspace_root(env),
        ),
        topic,
    )


async def run(
    source_dir: Path,
    overlay: ModelFloorProjectionPublishOverlay,
    publisher: ProtocolEnvelopePublisher,
    sink: ProtocolEmissionSink,
    *,
    producer: str,
    clock: Callable[[], datetime] | None = None,
) -> ModelFloorProjectionPublishResult:
    """One pass with its chain: a start, then a success with the entries published, or a typed failure."""
    contract = _contract()["runtime"]
    chain = ChainEmitter(
        sink, node=contract["chain"], piece=int(contract["piece"]), clock=clock
    )
    chain.start()
    handler = HandlerFloorOwnerMapPublish(
        read_owners=read_controller_owners,
        publisher=publisher,
        projection_id=overlay.projection_id,
        producer=producer,
        state_name=contract["state_name"],
        clock=clock,
    )
    try:
        result = await handler.handle(
            ModelFloorProjectionPublishRequest(source_dir=str(source_dir))
        )
    except Exception as exc:
        chain.failure("owner_map_publish_failed", type(exc).__name__)
        raise
    if result.published:
        chain.success({"owners_published": result.entries})
    else:
        chain.failure("controller_state_unreadable", result.reason)
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
            ModelFloorProjectionPublishOverlay,
            env,
            file_env=contract["overlay"]["file_env"],
        )
    except NodeOverlayError as exc:
        sys.stderr.write(f"{NODE}: {exc}\n")
        return EXIT_UNREADABLE
    source_dir = Path(
        env.get(contract["state_dir_env"], "").strip()
        or Path.home() / contract["state_dir_fallback"]
    )
    host = env.get("HOSTNAME", "") or os.uname().nodename
    try:
        result = asyncio.run(
            run(
                source_dir,
                overlay,
                build_publisher(overlay, env, contract["topic"]),
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


__all__ = ["BusEnvelopePublisher", "build_publisher", "main", "run"]
