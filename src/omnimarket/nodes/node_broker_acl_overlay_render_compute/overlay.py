# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load a scoped broker permission overlay at composition (OMN-23589)."""

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.broker_acl import (
    ModelBrokerAclPrincipalsOverlay,
    ModelBrokerAclScopeOverlay,
)


def load_scope_overlay(
    scope: str, environ: Mapping[str, str] | None = None
) -> ModelBrokerAclScopeOverlay:
    """Read <root>/<scope>/overlay.yaml under ONEX_SKILL_OVERLAY_ROOTS; a missing or invalid file refuses."""
    return load_structured_overlay(scope, ModelBrokerAclScopeOverlay, environ)


def load_principals_overlay(
    scope: str, environ: Mapping[str, str] | None = None
) -> ModelBrokerAclPrincipalsOverlay:
    """Read the declared principals (which login runs which nodes) the same way; a missing file raises not-found."""
    return load_structured_overlay(scope, ModelBrokerAclPrincipalsOverlay, environ)
