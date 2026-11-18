# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One fill tick for the scheduled job: read the fill overlay, build the fill on its profile, run a tick.

The scheduled job node reaches the floor fill orchestrator through this shared module,
so no node imports another node's model package.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_floor_fill_orchestrator.container.container_inmemory import (
    build_inmemory_handler,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    ModelFloorFillCommandRequest,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_overlay_document import (
    ModelFloorFillOverlayDocument,
)


class FloorFillProfileUnboundError(Exception):
    """The overlay names a profile the runtime does not bind yet."""


class ModelFloorFillTickOutcome(NamedTuple):
    """The exit code and the first stdout line of one fill tick."""

    exit_code: int
    first_line: str


def run_floor_fill_tick(
    *,
    fill_overlay_id: str,
    profile: str,
    environ: Mapping[str, str] | None,
    build_handler: Callable[[Any], Any] | None = None,
) -> ModelFloorFillTickOutcome:
    """Read the fill overlay and run one tick; raises ``NodeOverlayError`` when it cannot be read."""
    document = load_structured_overlay(
        fill_overlay_id, ModelFloorFillOverlayDocument, environ
    )
    config = document.floor_fill
    build = build_handler
    if build is None:
        if profile != "inmemory":
            raise FloorFillProfileUnboundError(profile)
        build = build_inmemory_handler
    result = build(config).handle(
        ModelFloorFillCommandRequest(
            command=EnumFloorFillCommand.TICK, key=None, host_id=config.host_id
        )
    )
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    return ModelFloorFillTickOutcome(result.exit_code, first_line)


__all__: list[str] = [
    "FloorFillProfileUnboundError",
    "ModelFloorFillTickOutcome",
    "run_floor_fill_tick",
]
