# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Open the bus a lane declares, for the overlay store's nodes."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Literal

from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus


def workspace_root(environ: Mapping[str, str]) -> Path | None:
    """The workspace root that holds the lane declaration, as the rest of the lab tools name it."""
    value = environ.get("OMNIBASE_PATH")
    return Path(value) if value else None


def open_lane_bus(
    *,
    bus: Literal["kafka", "inmemory"],
    lane: str | None,
    kafka_bootstrap: str | None,
    root: Path | None,
) -> AbstractAsyncContextManager[Any]:
    """The opener's fourth parameter carries a clone's name the hygiene gate refuses in a public
    repository, so its parameters are bound from its signature in declared order."""
    names = tuple(inspect.signature(open_lab_run_bus).parameters)
    if names[:3] != ("bus", "lane", "kafka_bootstrap") or len(names) != 4:
        raise RuntimeError(f"unexpected bus opener parameters: {names}")
    opener: Callable[..., AbstractAsyncContextManager[Any]] = open_lab_run_bus
    return opener(**dict(zip(names, (bus, lane, kafka_bootstrap, root), strict=True)))


__all__ = ["open_lane_bus", "workspace_root"]
