# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_stop_drill_effect: every bound and name, none in code.

Found at ``<root>/node_stop_drill_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml``. No key has a default.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_stop_drill_effect.models import ModelStopDrillOverlay

NODE_NAME = "node_stop_drill_effect"


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelStopDrillOverlay:
    return load_structured_overlay(NODE_NAME, ModelStopDrillOverlay, environ)
