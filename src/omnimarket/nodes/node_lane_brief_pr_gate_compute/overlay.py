# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``lane_brief_pr_gate`` overlay: the gate's mode and the citation that moved it.

Shape only; the deployment owner supplies the values. The caller loads the overlay once and
constructs the handler with the result; the handler never reads it. ``enforce`` with no
citation is refused naming the key.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.lane_brief_pr_gate.model_lane_brief_pr_gate_config import (
    ModelLaneBriefPrGateConfig,
)

OVERLAY_ID = "lane_brief_pr_gate"


def load_lane_brief_pr_gate_config(
    environ: Mapping[str, str] | None = None,
) -> ModelLaneBriefPrGateConfig:
    """Load and validate ``<root>/lane_brief_pr_gate/overlay.yaml``; refuses naming the missing key."""
    return load_structured_overlay(OVERLAY_ID, ModelLaneBriefPrGateConfig, environ)
