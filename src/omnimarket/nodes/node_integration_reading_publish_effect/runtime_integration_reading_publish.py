# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_integration_reading_publish_effect --home <registry home> < batch.json``.

The hourly integration pass pipes one ``ModelIntegrationReadingBatch`` (JSON) on stdin. This module composes: it
validates the batch, has ``node_integration_projection_compute`` build the projection, and hands the effect handler
one typed publish command. Exit 0 when the projection was published; 1 when the bus refused; 65 when the batch or the
overlay cannot be read (nothing is published, and the chain ends in a typed failure).
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import sys
from collections.abc import Callable
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.handlers.floor_projection import ProtocolSnapshotSource
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.floor_projection import (
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionPublishOverlay,
    ModelFloorProjectionPublishResult,
    ModelFloorProjectionReadOverlay,
)
from omnimarket.models.integration_reading import (
    ModelIntegrationProjection,
    ModelIntegrationPublishCommand,
    ModelIntegrationReadingBatch,
)
from omnimarket.nodes.node_floor_owner_map_publish_effect.handlers import (
    ProtocolEnvelopePublisher,
)
from omnimarket.nodes.node_floor_owner_map_publish_effect.runtime_floor_owner_map_publish import (
    build_publisher,
)
from omnimarket.nodes.node_floor_projection_read_effect.runtime_floor_projection_read import (
    build_source,
)
from omnimarket.nodes.node_integration_projection_compute.handlers import (
    HandlerIntegrationProjection,
)
from omnimarket.nodes.node_integration_reading_publish_effect.handlers import (
    HandlerIntegrationReadingPublish,
)

NODE = "node_integration_reading_publish_effect"
EXIT_NOT_PUBLISHED = 1
EXIT_UNREADABLE = 65


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return dict(yaml.safe_load(text))


def _chain(
    sink: ProtocolEmissionSink, clock: Callable[[], datetime] | None
) -> ChainEmitter:
    runtime = _contract()["runtime"]
    return ChainEmitter(
        sink,
        node=runtime["chain"],
        piece=int(runtime["piece"]),
        origin="real",
        clock=clock,
    )


def refuse(
    reason: str,
    detail: str,
    sink: ProtocolEmissionSink,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """A batch or overlay that cannot be read: a start and a typed failure, nothing published."""
    chain = _chain(sink, clock)
    chain.start()
    chain.failure(reason, detail[:500])


async def run(
    batch: ModelIntegrationReadingBatch,
    overlay: ModelFloorProjectionPublishOverlay,
    publisher: ProtocolEnvelopePublisher,
    sink: ProtocolEmissionSink,
    *,
    producer: str,
    clock: Callable[[], datetime] | None = None,
) -> ModelFloorProjectionPublishResult:
    """One pass with its chain: a start, then a success with the rows published, or a typed failure."""
    chain = _chain(sink, clock)
    chain.start()
    projection = HandlerIntegrationProjection().handle(batch)
    handler = HandlerIntegrationReadingPublish(publisher=publisher, producer=producer)
    try:
        result = await handler.handle(
            ModelIntegrationPublishCommand(
                projection_id=overlay.projection_id, projection=projection
            )
        )
    except Exception as exc:
        chain.failure("integration_publish_failed", type(exc).__name__)
        raise
    chain.success(
        {
            "rows_published": result.entries,
            "unintegrated": projection.counts.unintegrated,
        }
    )
    return result


async def retain_previous_readings(
    batch: ModelIntegrationReadingBatch,
    source: ProtocolSnapshotSource,
    *,
    topic: str,
    projection_id: str,
) -> ModelIntegrationReadingBatch:
    """Merge the new readings with the latest bus snapshot before replacing it."""
    latest = None
    for payload in await source.publications(topic):
        item = ModelFloorProjectionEnvelope.model_validate_json(payload)
        if item.projection_id == projection_id and (
            latest is None or item.as_of >= latest.as_of
        ):
            latest = item
    if latest is None:
        raise ValueError(
            "no existing integration snapshot was readable for incremental publication"
        )
    previous = ModelIntegrationProjection.model_validate(latest.payload)
    return ModelIntegrationReadingBatch(
        as_of=max(previous.as_of, batch.as_of),
        origin=batch.origin,
        readings=(*previous.rows.values(), *batch.readings),
    )


def _publish_incremental(
    *,
    home: Path,
    batch: ModelIntegrationReadingBatch,
    overlay: ModelFloorProjectionPublishOverlay,
    publisher: ProtocolEnvelopePublisher,
    sink: ProtocolEmissionSink,
    producer: str,
) -> ModelFloorProjectionPublishResult:
    runtime = _contract()["runtime"]
    config = runtime["incremental"]
    read_overlay = load_structured_overlay(
        config["overlay_id"],
        ModelFloorProjectionReadOverlay,
        os.environ,
        file_env=config["file_env"],
    )
    if (read_overlay.bus, read_overlay.kafka_bootstrap, read_overlay.bus_lane) != (
        overlay.bus,
        overlay.kafka_bootstrap,
        overlay.bus_lane,
    ):
        raise ValueError(
            "integration projection reader and publisher are on different buses"
        )
    source = build_source(read_overlay, os.environ, config["group_prefix"])

    async def publish() -> ModelFloorProjectionPublishResult:
        try:
            merged = await retain_previous_readings(
                batch,
                source,
                topic=runtime["topic"],
                projection_id=overlay.projection_id,
            )
        except Exception as exc:
            refuse("integration_previous_snapshot_unread", type(exc).__name__, sink)
            raise
        return await run(merged, overlay, publisher, sink, producer=producer)

    # Every ready-edge publisher on this worker shares one read-and-publish lock.
    # A process exit also releases it, so a timed-out attempt cannot strand the next read.
    lock = home / config["lock_relpath"]
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a", encoding="utf-8") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        return asyncio.run(publish())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE, description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Retain existing readings on the integration projection while applying this batch.",
    )
    args = parser.parse_args(argv)
    env = os.environ
    runtime = _contract()["runtime"]
    sink = JsonlEmissionSink(args.home / runtime["journal_relpath"])
    try:
        overlay = load_structured_overlay(
            runtime["overlay"]["overlay_id"],
            ModelFloorProjectionPublishOverlay,
            env,
            file_env=runtime["overlay"]["file_env"],
        )
    except NodeOverlayError as exc:
        refuse("integration_overlay_unreadable", str(exc), sink)
        sys.stderr.write(f"{NODE}: {exc}\n")
        return EXIT_UNREADABLE
    try:
        batch = ModelIntegrationReadingBatch.model_validate_json(sys.stdin.read())
    except (ValidationError, ValueError) as exc:
        refuse("integration_batch_invalid", str(exc), sink)
        sys.stderr.write(
            f"{NODE}: the batch is not a ModelIntegrationReadingBatch: {exc}\n"
        )
        return EXIT_UNREADABLE
    host = env.get("HOSTNAME", "") or os.uname().nodename
    try:
        publisher = build_publisher(overlay, env, runtime["topic"])
        producer = f"{NODE}:{host}"
        if args.incremental:
            result = _publish_incremental(
                home=args.home,
                batch=batch,
                overlay=overlay,
                publisher=publisher,
                sink=sink,
                producer=producer,
            )
        else:
            result = asyncio.run(
                run(batch, overlay, publisher, sink, producer=producer)
            )
    except Exception as exc:
        sys.stderr.write(f"{NODE}: {type(exc).__name__}: {exc}\n")
        return EXIT_NOT_PUBLISHED
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "refuse", "run"]
