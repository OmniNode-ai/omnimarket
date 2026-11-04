# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gathering settings of the work progress alert effect, read at evaluation time.

Settings come from ``<root>/node_work_progress_alert_effect/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``. Nothing is packaged, and nothing reads the overlay
while the runtime wires the node: a missing overlay refuses the evaluation.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_work_progress_alert_effect.models import (
    ModelWorkProgressAlertSettings,
)

OVERLAY_ID = NODE_NAME = "node_work_progress_alert_effect"


def load_settings(
    environ: Mapping[str, str] | None = None,
) -> ModelWorkProgressAlertSettings:
    """Raise ``NodeOverlayError`` naming the setting when the overlay is absent or invalid."""
    return load_structured_overlay(NODE_NAME, ModelWorkProgressAlertSettings, environ)


__all__ = ["NODE_NAME", "OVERLAY_ID", "load_settings"]
