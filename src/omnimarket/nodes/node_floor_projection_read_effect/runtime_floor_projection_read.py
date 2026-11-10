# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_floor_projection_read_effect --home <registry home>``.

Reads the projections the overlay names and writes the bundle the floor cycle reads. Exit 0 when the bundle was written,
even with projections unread (they are the reading); 65 when the overlay cannot be read; 1 when the bundle cannot be written.
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
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.models.floor_projection import (
    ModelFloorProjectionBundle,
    ModelFloorProjectionReadOverlay,
    ModelFloorProjectionTickResult,
)
from omnimarket.nodes.node_floor_projection_read_effect.handlers import (
    HandlerFloorProjectionRead,
)

NODE = "node_floor_projection_read_effect"
EXIT_UNREADABLE = 65
EXIT_NOT_WRITTEN = 1


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return dict(yaml.safe_load(text))


def build_source(
    overlay: ModelFloorProjectionReadOverlay,
    env: Mapping[str, str],
    group_prefix: str,
) -> ProtocolSnapshotSource:
    """The source the overlay selects: held in memory, or the broker."""
    if overlay.bus == "inmemory":
        return InMemorySnapshotSource()
    bootstrap = overlay.kafka_bootstrap
    return BusSnapshotSource(
        open_bus=lambda: open_lane_bus(
            bus="kafka",
            lane=None if bootstrap else overlay.bus_lane,
            kafka_bootstrap=bootstrap,
            root=workspace_root(env),
        ),
        group_prefix=group_prefix,
        window_s=overlay.read_window_s,
    )


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


async def run(
    home: Path,
    overlay: ModelFloorProjectionReadOverlay,
    source: ProtocolSnapshotSource,
    sink: ProtocolEmissionSink,
    *,
    clock: Callable[[], datetime] | None = None,
) -> ModelFloorProjectionTickResult:
    """One pass: read, record the chain, write the bundle. The chain ends in a failure when any projection is unread."""
    contract = _contract()["runtime"]
    chain = ChainEmitter(
        sink, node=contract["chain"], piece=int(contract["piece"]), clock=clock
    )
    chain.start()
    handler = HandlerFloorProjectionRead(source=source, overlay=overlay, clock=clock)
    try:
        bundle: ModelFloorProjectionBundle = await handler.handle()
    except Exception as exc:
        chain.failure("projection_read_failed", type(exc).__name__)
        raise
    unread = {k: e for k, e in bundle.entries.items() if e.status != "read"}
    output = home / contract["output_relpath"]
    try:
        _write_atomically(output, bundle.model_dump_json(indent=2))
    except OSError as exc:
        chain.failure("bundle_not_written", type(exc).__name__)
        raise
    if unread:
        chain.failure(
            "projection_unread",
            "; ".join(f"{k}: {e.reason}" for k, e in unread.items()),
        )
    else:
        chain.success({"projections_read": len(bundle.entries)})
    return ModelFloorProjectionTickResult(
        output_path=str(output),
        read=len(bundle.entries) - len(unread),
        unread=len(unread),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE, description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    args = parser.parse_args(argv)
    env = os.environ
    contract = _contract()["runtime"]
    try:
        overlay = load_structured_overlay(
            contract["overlay"]["overlay_id"],
            ModelFloorProjectionReadOverlay,
            env,
            file_env=contract["overlay"]["file_env"],
        )
    except NodeOverlayError as exc:
        sys.stderr.write(f"{NODE}: {exc}\n")
        return EXIT_UNREADABLE
    sink = JsonlEmissionSink(args.home / contract["journal_relpath"])
    try:
        result = asyncio.run(
            run(
                args.home,
                overlay,
                build_source(overlay, env, contract["group_prefix"]),
                sink,
                clock=lambda: datetime.now(UTC),
            )
        )
    except Exception as exc:
        sys.stderr.write(f"{NODE}: {type(exc).__name__}: {exc}\n")
        return EXIT_NOT_WRITTEN
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["build_source", "main", "run"]
