# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-backed plugin walk evidence effect."""

from omnimarket.nodes.node_plugin_walk_evidence_effect.handlers.handler_plugin_walk_evidence import (
    HandlerPluginWalkEvidence,
)


class NodePluginWalkEvidenceEffect(HandlerPluginWalkEvidence):
    """ONEX entry point."""


__all__ = ["HandlerPluginWalkEvidence", "NodePluginWalkEvidenceEffect"]
