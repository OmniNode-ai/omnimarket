# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Loader of the closeout scope overlay (OMN-20655)."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import resolve_overlay_file
from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_closeout_scope_overlay import (
    ModelCloseoutScopeOverlay,
)

PACKAGED_OVERLAY = Path(__file__).resolve().parent.parent / "overlay.yaml"
NODE_NAME = "node_dod_closeout_sweep_compute"
OVERLAY_FILE_ENV = "OMNIMARKET_CLOSEOUT_SCOPE_OVERLAY"


def load_scope_overlay(path: Path | None = None) -> ModelCloseoutScopeOverlay:
    """The overlay at ``path``, else the deployed overlay (the file OMNIMARKET_CLOSEOUT_SCOPE_OVERLAY
    names, else ``<root>/node_dod_closeout_sweep_compute/overlay.yaml`` under the first root in
    ONEX_SKILL_OVERLAY_ROOTS that holds one), else the node's packaged overlay.yaml. A missing,
    unreadable or invalid overlay raises; nothing falls back to a value written in code."""
    source = path or resolve_overlay_file(
        NODE_NAME, PACKAGED_OVERLAY, env_var=OVERLAY_FILE_ENV
    )
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    return ModelCloseoutScopeOverlay.model_validate(data)
