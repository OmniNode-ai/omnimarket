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
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.floor_projection import (
    ModelFloorProjectionPublishOverlay,
    ModelFloorProjectionPublishResult,
)
from omnimarket.models.integration_reading import (
    ModelIntegrationPublishCommand,
    ModelIntegrationReadingBatch,
)
from omnimarket.nodes.node_floor_owner_map_publish_effect.handlers import (
    ProtocolEnvelopePublisher,
)
from omnimarket.nodes.node_floor_owner_map_publish_effect.runtime_floor_owner_map_publish import (
    build_publisher,
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
        sink, node=runtime["chain"], piece=int(runtime["piece"]), clock=clock
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE, description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
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
        result = asyncio.run(
            run(
                batch,
                overlay,
                build_publisher(overlay, env, runtime["topic"]),
                sink,
                producer=f"{NODE}:{host}",
            )
        )
    except Exception as exc:
        sys.stderr.write(f"{NODE}: {type(exc).__name__}: {exc}\n")
        return EXIT_NOT_PUBLISHED
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "refuse", "run"]
