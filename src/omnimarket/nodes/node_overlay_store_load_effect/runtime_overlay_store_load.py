# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_overlay_store_load_effect``: load overlays of the home onto the bus."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from importlib import resources
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.models.overlay_store import (
    ModelOverlayStoreLoadOverlay,
    ModelOverlayStoreLoadResult,
)
from omnimarket.nodes.node_overlay_store_load_effect.handlers import (
    HandlerOverlayStoreLoad,
    OverlayHomeSource,
)

NODE = "node_overlay_store_load_effect"


async def _run(
    overlay: ModelOverlayStoreLoadOverlay,
    clone: Path,
    ids: list[str],
    kafka_bootstrap: str | None,
) -> list[ModelOverlayStoreLoadResult]:
    contract = yaml.safe_load(
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    bootstrap = kafka_bootstrap or overlay.kafka_bootstrap
    async with open_lane_bus(
        bus=overlay.bus,
        lane=None if bootstrap or overlay.bus == "inmemory" else overlay.bus_lane,
        kafka_bootstrap=bootstrap,
        root=workspace_root(os.environ),
    ) as bus:
        handler = HandlerOverlayStoreLoad(
            source=OverlayHomeSource(clone),
            bus=bus,
            topic=contract["event_bus"]["publish_topics"][0],
            loaded_by=overlay.loaded_by,
        )
        return [await handler.handle(overlay_id) for overlay_id in ids]


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
        results = asyncio.run(_run(overlay, args.clone, args.id, args.kafka_bootstrap))
    except (NodeOverlayError, ValueError) as exc:
        print(f"{NODE}: {type(exc).__name__}: {exc}", file=sys.stderr)  # noqa: T201
        return 65
    for result in results:
        print(result.model_dump_json())  # noqa: T201
    return 0


__all__ = ["main"]
