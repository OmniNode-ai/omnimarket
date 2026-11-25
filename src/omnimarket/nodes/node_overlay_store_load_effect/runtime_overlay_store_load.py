# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_overlay_store_load_effect``: load overlays of the home onto the bus."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from contextlib import AbstractAsyncContextManager
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.handlers.overlay_store.store_bus_snapshot import BusSnapshotOverlayStore
from omnimarket.models.overlay_store import (
    ModelOverlayAssertionStaleAlarm,
    ModelOverlayStoreLoadBatch,
    ModelOverlayStoreLoadOverlay,
)
from omnimarket.nodes.node_overlay_store_load_effect.handlers import (
    HandlerOverlayStoreLoad,
    OverlayHomeSource,
)

NODE = "node_overlay_store_load_effect"


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    contract: dict[str, Any] = yaml.safe_load(text)
    return contract


def _alarm_topic(contract: dict[str, Any]) -> str:
    """The contract's publish topic that carries a stale-assertion alarm."""
    topics: list[str] = contract["event_bus"]["publish_topics"]
    return next(t for t in topics if "assertion-stale" in t)


def _open(
    overlay: ModelOverlayStoreLoadOverlay, kafka_bootstrap: str | None
) -> AbstractAsyncContextManager[Any]:
    bootstrap = kafka_bootstrap or overlay.kafka_bootstrap
    return open_lane_bus(
        bus=overlay.bus,
        lane=None if bootstrap or overlay.bus == "inmemory" else overlay.bus_lane,
        kafka_bootstrap=bootstrap,
        root=workspace_root(os.environ),
    )


async def _run(
    overlay: ModelOverlayStoreLoadOverlay,
    clone: Path,
    ids: list[str],
    kafka_bootstrap: str | None,
) -> ModelOverlayStoreLoadBatch:
    """Load each id in order; a refused id is named in the batch and the ids after it still publish."""
    contract = _contract()

    def open_bus() -> AbstractAsyncContextManager[Any]:
        return _open(overlay, kafka_bootstrap)

    topics = {_name_of(t): t for t in contract["event_bus"]["publish_topics"]}
    # A run reads the live record only where the overlay says how long to listen for it (OMN-26495).
    live = (
        BusSnapshotOverlayStore(
            open_bus=open_bus,
            topic=contract["snapshot_read"]["topic"],
            group_prefix=f"{NODE}.live",
            window_s=overlay.live_read_window_s,
        )
        if overlay.live_read_window_s is not None
        else None
    )
    # One id for the run, so every load, drift report and refusal of it reads as one chain.
    run = str(uuid.uuid4())
    async with open_bus() as bus:
        handler = HandlerOverlayStoreLoad(
            source=OverlayHomeSource(clone),
            bus=bus,
            topic=contract["event_bus"]["publish_topics"][0],
            loaded_by=overlay.loaded_by,
            live=live,
            drift_topic=topics["overlay-store-drift"],
            refusal_topic=topics["overlay-store-refused"],
        )
        return await handler.handle_many(ids, correlation_id=run)


async def _alarm(
    overlay: ModelOverlayStoreLoadOverlay,
    alarms: list[ModelOverlayAssertionStaleAlarm],
    kafka_bootstrap: str | None,
) -> None:
    """Publish one alarm per stale id, keyed by the id, on the topic the contract declares."""
    topic = _alarm_topic(_contract())
    async with _open(overlay, kafka_bootstrap) as bus:
        for alarm in alarms:
            await bus.publish(
                topic,
                alarm.overlay_id.encode("utf-8"),
                alarm.model_dump_json().encode("utf-8"),
            )


def _name_of(topic: str) -> str:
    """``overlay-store-drift`` of ``onex.evt.omnimarket.overlay-store-drift.v1``."""
    return topic.rsplit(".", 2)[-2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE, description=__doc__)
    parser.add_argument(
        "--clone", required=True, type=Path, help="a clone of the overlay home"
    )
    parser.add_argument("--id", action="append", required=True, help="an overlay id")
    parser.add_argument("--kafka-bootstrap", default=None)
    args = parser.parse_args(argv)
    try:
        overlay = load_structured_overlay(
            NODE, ModelOverlayStoreLoadOverlay, os.environ
        )
        batch = asyncio.run(_run(overlay, args.clone, args.id, args.kafka_bootstrap))
    except (NodeOverlayError, ValueError) as exc:
        sys.stderr.write(f"{NODE}: {type(exc).__name__}: {exc}\n")
        return 65
    for result in batch.results:
        sys.stdout.write(result.model_dump_json() + "\n")
    for refusal in batch.refused:
        sys.stderr.write(f"{NODE}: refused {refusal.overlay_id}: {refusal.reason}\n")
    return 65 if batch.refused else 0


__all__ = ["main"]
