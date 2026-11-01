# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Composition adapter: the dashboard a demo node's probes read.

The address is a deployment fact. An explicit overlay wins, then the ``DEMO_DASHBOARD_URL``
setting, then ``<root>/<node name>/overlay.yaml``. When none supplies it the node refuses
with an error naming the setting; the package ships no address.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    load_structured_overlay,
    not_configured_message,
)
from omnimarket.models.model_demo_probe import ModelDemoProbeOverlay

DASHBOARD_URL_ENV = "DEMO_DASHBOARD_URL"


def resolve_demo_probe_overlay(
    node_name: str,
    *,
    explicit: ModelDemoProbeOverlay | None = None,
    environ: Mapping[str, str] | None = None,
) -> ModelDemoProbeOverlay:
    """The probe overlay of ``node_name``; raises ``NodeOverlayError`` when nothing supplies it."""
    env = os.environ if environ is None else environ
    supplied: ModelDemoProbeOverlay | dict[str, str] | None = explicit
    if supplied is None:
        url = env.get(DASHBOARD_URL_ENV, "").strip()
        if url:
            supplied = {"dashboard_url": url}
    return load_structured_overlay(
        node_name,
        ModelDemoProbeOverlay,
        env,
        explicit=supplied,
        missing_message=not_configured_message(
            node_name, f"{node_name}.dashboard_url", (DASHBOARD_URL_ENV,)
        ),
    )


__all__ = ["DASHBOARD_URL_ENV", "resolve_demo_probe_overlay"]
