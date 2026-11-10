# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load a broker ACL overlay by id through the structured overlay reader."""

from __future__ import annotations

import re
from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_broker_acl_overlay_render_compute.models.model_broker_acl_overlay import (
    ModelBrokerAclOverlay,
)

OVERLAY_ID_PATTERN = r"^[a-z0-9_]+$"


def load_acl_overlay(
    overlay_id: str, environ: Mapping[str, str] | None = None
) -> ModelBrokerAclOverlay:
    """Validate the overlay id and load the overlay as a typed model."""
    if not re.fullmatch(OVERLAY_ID_PATTERN, overlay_id):
        raise ValueError(
            f"overlay id {overlay_id!r} does not match {OVERLAY_ID_PATTERN!r}"
        )
    return load_structured_overlay(overlay_id, ModelBrokerAclOverlay, environ)
