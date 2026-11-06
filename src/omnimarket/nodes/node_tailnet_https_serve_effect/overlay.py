# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_tailnet_https_serve_effect: every deployment fact, none in code.

Found at ``<root>/node_tailnet_https_serve_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``, or at the file ``ONEX_TAILNET_HTTPS_SERVE_OVERLAY`` names. The package ships
``overlay.example.yaml`` with synthetic values on the in-memory backend. No key has a default.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_tailnet_https_serve_effect.models import ModelServeOverlay

NODE_NAME = "node_tailnet_https_serve_effect"
OVERLAY_FILE_ENV = "ONEX_TAILNET_HTTPS_SERVE_OVERLAY"


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelServeOverlay:
    return load_structured_overlay(
        NODE_NAME, ModelServeOverlay, environ, file_env=OVERLAY_FILE_ENV
    )
