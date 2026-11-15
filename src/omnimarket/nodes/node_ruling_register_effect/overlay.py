# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of ``node_ruling_register_effect``: the read route, none in code.

Found at ``<root>/node_ruling_register_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml``. No key has a
default.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_ruling_register_effect.models import ModelRulingRoute

NODE_NAME = "node_ruling_register_effect"


def load_route(environ: Mapping[str, str] | None = None) -> ModelRulingRoute:
    """Load and validate the route overlay; a missing key or a bad value is refused naming it."""
    return load_structured_overlay(NODE_NAME, ModelRulingRoute, environ)


__all__ = ["NODE_NAME", "load_route"]
