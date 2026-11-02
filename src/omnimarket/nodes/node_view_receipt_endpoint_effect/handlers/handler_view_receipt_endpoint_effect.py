# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""INSUFFICIENT scaffold of the publish effect's receipt endpoint (VS-08, VS-12 and VS-16 receiver).

The seam graph names this node as an emitter before the step that builds it lands, so that every
seam side has a declared producer. Until then it returns a typed INSUFFICIENT result and publishes
nothing; the seams it serves read unreadable, never matched.
"""

from __future__ import annotations

from omnimarket.models.seam_reading import (
    ModelSeamEmitterInsufficient,
    ModelSeamEmitterRequest,
)

__all__ = ["HandlerViewReceiptEndpointEffect", "ModelSeamEmitterRequest"]

NODE_NAME = "node_view_receipt_endpoint_effect"


class HandlerViewReceiptEndpointEffect:
    def handle(self, request: ModelSeamEmitterRequest) -> ModelSeamEmitterInsufficient:
        return ModelSeamEmitterInsufficient(
            emitter=NODE_NAME,
            seam_id=request.seam_id,
            side=request.side,
            reason="publish effect's receipt endpoint is not built yet: it emits no count or window close for VS-08, VS-12 and VS-16 receiver",
        )
