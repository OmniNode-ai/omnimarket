# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_private_hosted_minutes_watch_effect: no value has a default.

Found at ``<root>/node_private_hosted_minutes_watch_effect/overlay.yaml`` under a root
named in ``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml``.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
    load_structured_overlay,
)

from .models import ModelHostedMinutesOverlay

NODE_NAME = "node_private_hosted_minutes_watch_effect"


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelHostedMinutesOverlay:
    try:
        return load_structured_overlay(NODE_NAME, ModelHostedMinutesOverlay, environ)
    except NodeOverlayError as exc:
        message = str(exc)
        if OVERLAY_ROOTS_ENV not in message:
            message += (
                f"; set {OVERLAY_ROOTS_ENV} to a root holding {NODE_NAME}/overlay.yaml"
            )
        raise NodeOverlayError(message) from None
