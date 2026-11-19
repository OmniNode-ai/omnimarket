# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pr_landing_liveness_read_effect: the automation-liveness read of one PR head for the landing decision (OMN-23529)."""

from collections.abc import Mapping

from omnimarket.nodes.node_pr_landing_liveness_read_effect.handlers.handler_pr_landing_liveness_read import (
    HandlerPrLandingLivenessRead,
    build_reader,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.models.model_liveness_read import (
    ModelLivenessReadRequest,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.overlay import load_overlay
from omnimarket.nodes.node_pr_landing_liveness_read_effect.protocols.protocol_liveness_read import (
    ProtocolLivenessReadPublisher,
)


def build_liveness_read(
    environ: Mapping[str, str], publisher: ProtocolLivenessReadPublisher
) -> HandlerPrLandingLivenessRead:
    """Bind the declared overlay, exact-head reader and event publisher."""
    overlay = load_overlay(environ)
    return HandlerPrLandingLivenessRead(
        overlay, build_reader(overlay, environ), publisher
    )


__all__ = ["ModelLivenessReadRequest", "build_liveness_read"]
