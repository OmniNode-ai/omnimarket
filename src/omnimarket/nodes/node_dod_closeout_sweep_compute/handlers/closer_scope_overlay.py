# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Loader of the closeout scope overlay (OMN-20655)."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_closeout_scope_overlay import (
    ModelCloseoutScopeOverlay,
)

PACKAGED_OVERLAY = Path(__file__).resolve().parent.parent / "overlay.yaml"


def load_scope_overlay(path: Path | None = None) -> ModelCloseoutScopeOverlay:
    """The overlay at ``path``, else the node's own overlay.yaml. A missing, unreadable or
    invalid overlay raises; nothing falls back to a value written in code."""
    source = path or PACKAGED_OVERLAY
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    return ModelCloseoutScopeOverlay.model_validate(data)
