# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load a scoped broker permission overlay at composition (OMN-23589)."""

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_broker_acl_overlay_render_compute.models import (
    ModelBrokerAclScopeOverlay,
)


def load_scope_overlay(
    scope: str, environ: Mapping[str, str] | None = None
) -> ModelBrokerAclScopeOverlay:
    """Read <root>/<scope>/overlay.yaml under ONEX_SKILL_OVERLAY_ROOTS; a missing or invalid file refuses."""
    return load_structured_overlay(scope, ModelBrokerAclScopeOverlay, environ)
