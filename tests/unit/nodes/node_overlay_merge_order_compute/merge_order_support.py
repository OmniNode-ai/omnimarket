# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared facts of the overlay merge order tests: the overlay, the merged tree's answers, the topics."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift import (
    EnumOverlayVerdict,
    ModelOverlayCheck,
)
from omnimarket.nodes.node_overlay_merge_order_compute.handlers.handler_overlay_merge_order import (
    HandlerOverlayMergeOrder,
)
from omnimarket.nodes.node_overlay_merge_order_compute.models.model_overlay_merge_order import (
    ModelOverlayMergeOrderCommand,
)
from omnimarket.nodes.node_overlay_merge_order_compute.overlay import (
    ModelOverlayMergeOrderOverlay,
)

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_overlay_merge_order_compute"
)
CONTRACT = NODE_DIR / "contract.yaml"
EXAMPLE_OVERLAY = NODE_DIR / "overlay.example.yaml"
TOPICS: dict[str, str] = dict(
    yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["topics"]
)
CORRELATION = "chain-1"
MERGED_REF = "origin/dev"
MAX_NAMED = 2


def overlay(**over: Any) -> ModelOverlayMergeOrderOverlay:
    base: dict[str, Any] = {
        "ahead_verdicts": [
            "rejected",
            "model_missing",
            "unreadable",
            "overlay_missing",
        ],
        "max_named": MAX_NAMED,
        "injected_fault": "a touched overlay carries a key the merged model refuses",
    }
    base.update(over)
    return ModelOverlayMergeOrderOverlay.model_validate(base)


def check(
    overlay_id: str,
    verdict: EnumOverlayVerdict = EnumOverlayVerdict.ACCEPTED,
    *fields: str,
) -> ModelOverlayCheck:
    return ModelOverlayCheck(
        overlay_id=overlay_id, verdict=verdict, fields=tuple(fields)
    )


def command(
    changed: tuple[str, ...], checks: tuple[ModelOverlayCheck, ...]
) -> ModelOverlayMergeOrderCommand:
    return ModelOverlayMergeOrderCommand(
        correlation_id=CORRELATION,
        merged_ref=MERGED_REF,
        changed_overlays=changed,
        merged_checks=checks,
    )


def handler(**over: Any) -> HandlerOverlayMergeOrder:
    return HandlerOverlayMergeOrder(overlay(**over))


def whole_home() -> tuple[ModelOverlayCheck, ...]:
    """A home of four overlays: two accepted, one the merged model refuses, one whose model is absent."""
    return (
        check("node_alpha_effect"),
        check("node_beta_compute"),
        check(
            "node_gamma_effect",
            EnumOverlayVerdict.REJECTED,
            "clone_branch",
            "overlay_ids",
        ),
        check("node_delta_effect", EnumOverlayVerdict.MODEL_MISSING),
    )
