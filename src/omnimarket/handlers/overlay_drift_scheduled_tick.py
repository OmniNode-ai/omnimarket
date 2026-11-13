# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the scheduled job: read the drift overlay, build the drift handler, run one read.

The scheduled job node reaches the overlay drift read through this shared module, so no node imports another
node's models package. The read's rules are node_overlay_drift_effect's own overlay (``<drift_overlay_id>/overlay.yaml``
under ``ONEX_SKILL_OVERLAY_ROOTS``). An unreadable overlay raises ``NodeOverlayError``; an environment variable the
overlay names that is not set, or a source that is not bound, raises ``ValueError``, ``OSError`` or
``LedgerRouteError`` — the scheduled job node turns both into failed run events.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, NamedTuple

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_overlay_drift_effect.handlers.upstream_chain import (
    newest_unanswered_upstream_cid,
)
from omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift import (
    ModelOverlayDriftCommand,
)
from omnimarket.nodes.node_overlay_drift_effect.overlay import ModelOverlayDriftOverlay
from omnimarket.nodes.node_overlay_drift_effect.runtime_overlay_drift import (
    build_handler,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_row_source,
)


class ModelOverlayDriftTickOutcome(NamedTuple):
    """How one scheduled drift read went: failed, and the detail the run event carries."""

    failed: bool
    detail: str


def _upstream_cid(
    overlay: ModelOverlayDriftOverlay,
    environ: Mapping[str, str],
    clock: Callable[[], datetime],
) -> str | None:
    """The upstream chain this run answers (join J3), or ``None`` when the overlay names no upstream.

    The way the drift runtime's ``_upstream_cid`` resolves it, from the given environ and clock
    instead of ``os.environ`` and ``now()``; a ledger read that cannot tell claims no join.
    """
    if overlay.upstream is None:
        return None
    return newest_unanswered_upstream_cid(
        build_row_source(overlay.upstream.ledger_read, environ),
        overlay.upstream,
        overlay.publish.lane,
        clock(),
    )


def run_overlay_drift_tick(
    *,
    drift_overlay_id: str,
    environ: Mapping[str, str] | None,
    clock: Callable[[], datetime],
    build: Callable[..., Any] | None = None,
) -> ModelOverlayDriftTickOutcome:
    """Run the drift read once, the way the drift runtime's daily verb does.

    ``build`` stands in for the drift runtime's ``build_handler`` so tests inject a fake
    handler and no real home, runtime, git or ledger adapter is built.
    """
    overlay = load_structured_overlay(
        drift_overlay_id, ModelOverlayDriftOverlay, environ
    )
    env = os.environ if environ is None else environ
    build_one = build if build is not None else build_handler
    handler, host = build_one(overlay, env, True)
    stamp = clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelOverlayDriftCommand(
            correlation_id=f"overlay-drift-{host}-{stamp}",
            write=True,
            upstream_cid=_upstream_cid(overlay, env, clock),
        )
    )
    counts = result.counts
    if result.status == "failed":
        return ModelOverlayDriftTickOutcome(True, result.failure or "")
    return ModelOverlayDriftTickOutcome(
        result.failed,
        f"verdict={result.verdict.value} checked={counts.checked} "
        f"accepted={counts.accepted} rejected={counts.rejected}",
    )


__all__: list[str] = [
    "LedgerRouteError",
    "ModelOverlayDriftTickOutcome",
    "run_overlay_drift_tick",
]
