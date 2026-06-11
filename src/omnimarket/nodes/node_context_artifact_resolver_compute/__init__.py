# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_context_artifact_resolver_compute: materialise real per-factor content.

Pure COMPUTE node that turns pre-read artifact sources into the
``artifact_content_map`` the context-ROI runner (OMN-12798) injects, reusing
the existing GuidanceSectionParser (OMN-12795) and the pack-builder's
budget/precedence authority.
"""

from omnimarket.nodes.node_context_artifact_resolver_compute.handlers.handler_artifact_resolver import (
    HandlerArtifactResolver,
)

__all__ = ["HandlerArtifactResolver"]
