# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the scheduled job: read the scan's overlays, build the scan's handler, run one writing run.

The scheduled job node reaches the delegation_events defect scan effect through this shared module, so no node
imports another node's models package. The scan's routes are the effect's own overlay (``<scan_effect_overlay_id>/
overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``); the day, the grace and the row words are the compute node's scan
overlay (``<scan_compute_overlay_id>/overlay.yaml`` beside it). An unreadable overlay raises ``NodeOverlayError``
naming which one; a route the scan cannot bind raises ``ValueError``, ``OSError``, ``KeyError`` or
``LedgerRouteError``; the scheduled job node turns both into failed run events. A run that reads NOT WORKING is
still a completed run: the scan ran and wrote its row.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, NamedTuple

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_delegation_events_defect_scan import ModelDefectScanOverlay
from omnimarket.nodes.node_delegation_events_defect_scan_effect.models.model_defect_scan_run import (
    ModelDefectScanCommand,
)
from omnimarket.nodes.node_delegation_events_defect_scan_effect.overlay import (
    ModelDefectScanEffectOverlay,
)
from omnimarket.nodes.node_delegation_events_defect_scan_effect.runtime_delegation_events_defect_scan import (
    build_handler,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
)


class ModelDefectScanTickOutcome(NamedTuple):
    """How one scheduled run went: failed, and the detail the run event carries."""

    failed: bool
    detail: str


def run_defect_scan_tick(
    *,
    scan_effect_overlay_id: str,
    scan_compute_overlay_id: str,
    environ: Mapping[str, str] | None,
    clock: Callable[[], datetime],
    build: Callable[..., Any] | None = None,
) -> ModelDefectScanTickOutcome:
    """Run the defect scan's writing run once, the way the scan unit does."""
    env = os.environ if environ is None else environ
    overlay = load_structured_overlay(
        scan_effect_overlay_id, ModelDefectScanEffectOverlay, env
    )
    scan_overlay = load_structured_overlay(
        scan_compute_overlay_id, ModelDefectScanOverlay, env
    )
    handler = (
        build(overlay, scan_overlay, env, True)
        if build is not None
        else build_handler(overlay, scan_overlay, env, True)
    )
    result = handler.handle(
        ModelDefectScanCommand(
            correlation_id=f"defect-scan-{clock().strftime('%Y-%m-%dT%H:%M:%SZ')}",
            write=True,
        )
    )
    if result.status == "failed":
        return ModelDefectScanTickOutcome(True, result.failure or "")
    detail = (
        f"verdict={result.verdict.value} rows={result.rows_scanned} "
        f"missing_terminal={result.missing_terminal} unattributed={result.unattributed}"
    )
    return ModelDefectScanTickOutcome(False, detail)


__all__: list[str] = [
    "LedgerRouteError",
    "ModelDefectScanTickOutcome",
    "run_defect_scan_tick",
]
