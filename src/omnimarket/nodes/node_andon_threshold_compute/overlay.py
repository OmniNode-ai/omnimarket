# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``andon_threshold`` overlay: the frozen per-class thresholds.

The values, their hash and the recalibration date belong to the deployment owner and are
written by ``HandlerThresholdCompute`` (``overlay_yaml``). Loading refuses a values change
that carries no new hash.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_andon_threshold import ModelAndonThresholdFreeze

OVERLAY_ID = "andon_threshold"


def load_threshold_freeze(
    environ: Mapping[str, str] | None = None,
) -> ModelAndonThresholdFreeze:
    """Load and validate the frozen thresholds; refuses a value that does not match the hash."""
    return load_structured_overlay(OVERLAY_ID, ModelAndonThresholdFreeze, environ)
