# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``host_capacity`` overlay: load per core, free memory, swap and slot limits (OMN-23337).

Shape only here; the values are the deployment owner's, in ``<root>/host_capacity/overlay.yaml``. Every
key is required except ``hosts``, so a missing limit refuses naming the key and nothing falls back to a
number written in code.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_host_capacity import (
    ModelHostCapacityLimits,
)

OVERLAY_ID = "host_capacity"


def load_host_capacity_limits(
    environ: Mapping[str, str] | None = None,
) -> ModelHostCapacityLimits:
    """Load and validate the overlay; refuses naming the missing or invalid key."""
    return load_structured_overlay(OVERLAY_ID, ModelHostCapacityLimits, environ)
