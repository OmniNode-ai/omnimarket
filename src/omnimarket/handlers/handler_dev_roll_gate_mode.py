# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read whether the dev roll gate overlay says live (OMN-24431)."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.models.dev_roll.model_dev_roll_gate_mode import ModelDevRollGateMode

GATE_OVERLAY_ID = "node_dev_roll_gate_effect"


def dev_roll_gate_is_live(environ: Mapping[str, str] | None = None) -> bool:
    """True only when the gate's overlay is present, valid and says ``live``.

    An absent overlay reads as off, so a deployment that never installed the gate keeps
    publishing dev commands straight to the deploy agent. A present overlay that is
    broken raises: a broken overlay must not read as an absent one.
    """
    try:
        mode = load_structured_overlay(GATE_OVERLAY_ID, ModelDevRollGateMode, environ)
    except NodeOverlayNotFoundError:
        return False
    return mode.mode == "live"
