# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one overlay consumer join check on the runtime schedule."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, NamedTuple

from omnimarket.nodes.node_overlay_drift_compute.models.model_overlay_drift_compute import (
    ModelOverlayJoinCommand,
)
from omnimarket.nodes.node_overlay_drift_compute.overlay import load_overlay
from omnimarket.nodes.node_overlay_drift_compute.runtime_overlay_drift_compute import (
    build_handler,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
)


class ModelOverlayConsumerJoinTickOutcome(NamedTuple):
    """The failure state and detail of one scheduled join check."""

    failed: bool
    detail: str


def run_overlay_consumer_join_tick(
    *,
    environ: Mapping[str, str] | None,
    clock: Callable[[], datetime],
    build: Callable[..., Any] | None = None,
) -> ModelOverlayConsumerJoinTickOutcome:
    """Load the compute overlay and run its join check once."""
    overlay = load_overlay(environ)
    env = os.environ if environ is None else environ
    build_one = build if build is not None else build_handler
    handler, host = build_one(overlay, env, True)
    stamp = clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelOverlayJoinCommand(
            correlation_id=f"overlay-consumer-join-{host}-{stamp}", write=True
        )
    )
    if result.status == "failed":
        return ModelOverlayConsumerJoinTickOutcome(True, result.failure or "")
    return ModelOverlayConsumerJoinTickOutcome(
        result.failed,
        f"verdict={result.verdict.value} outcome={result.outcome.value} "
        f"consumers_checked={result.consumers_checked} "
        f"mismatches={len(result.mismatches)} park_lists={len(result.park_lists)} "
        f"unreadable={len(result.unreadable)}",
    )


__all__ = [
    "LedgerRouteError",
    "ModelOverlayConsumerJoinTickOutcome",
    "run_overlay_consumer_join_tick",
]
