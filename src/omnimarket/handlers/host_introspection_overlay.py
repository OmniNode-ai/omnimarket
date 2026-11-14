# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``host_introspection`` overlay: cadence, probes, timeouts and account tools (OMN-19998).

Shape only here; the values are the deployment owner's, in ``<root>/host_introspection/overlay.yaml``. Every key
without a default is required, so a missing value refuses naming the key and nothing falls back to a number written
in code.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_host_introspection_request import (
    ModelHostIntrospectionOverlay,
)

OVERLAY_ID = "host_introspection"


def load_host_introspection_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelHostIntrospectionOverlay:
    """Load and validate the overlay; refuses naming the missing or invalid key."""
    return load_structured_overlay(OVERLAY_ID, ModelHostIntrospectionOverlay, environ)
