# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One fill tick for the scheduled job: read the fill overlay, build the fill on its profile, run a tick.

The scheduled job node reaches the floor fill orchestrator through this shared module,
so no node imports another node's model package.
"""

from __future__ import annotations

import os
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


class FloorFillStoreDsnUnsetError(Exception):
    """The live profile's store DSN variable is unset in the environment."""

    def __init__(self, variable: str) -> None:
        self.variable = variable
        super().__init__(variable)


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
    """Read the fill overlay and run one tick; raises ``NodeOverlayError`` when it cannot be read, ``FloorFillProfileUnboundError`` for a profile the runtime does not bind, and ``FloorFillStoreDsnUnsetError`` when the live profile's store DSN variable is unset."""
    document = load_structured_overlay(
        fill_overlay_id, ModelFloorFillOverlayDocument, environ
    )
    config = document.floor_fill
    build = build_handler
    if build is None:
        if profile == "inmemory":
            build = build_inmemory_handler
        elif profile == "live":
            live = document.floor_fill_live
            name = live.store_dsn_env
            dsn = (os.environ if environ is None else environ).get(name, "")
            if not dsn:
                raise FloorFillStoreDsnUnsetError(name)
            from omnimarket.nodes.node_floor_fill_orchestrator.container.container_live import (
                build_live_handler_factory,
            )

            build = build_live_handler_factory(live, dsn=dsn)
        else:
            raise FloorFillProfileUnboundError(profile)
    result = build(config).handle(
        ModelFloorFillCommandRequest(
            command=EnumFloorFillCommand.TICK, key=None, host_id=config.host_id
        )
    )
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    return ModelFloorFillTickOutcome(result.exit_code, first_line)


__all__: list[str] = [
    "FloorFillProfileUnboundError",
    "FloorFillStoreDsnUnsetError",
    "ModelFloorFillTickOutcome",
    "run_floor_fill_tick",
]
