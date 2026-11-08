# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""INSUFFICIENT scaffold of the status line consumer's receipt (VS-14 receiver).

The seam graph names this node as an emitter before the step that builds it lands, so that every
seam side has a declared producer. Until then it returns a typed INSUFFICIENT result and publishes
nothing; the seams it serves read unreadable, never matched.
"""

from __future__ import annotations

from omnimarket.models.seam_reading import (
    ModelSeamEmitterInsufficient,
    ModelSeamEmitterRequest,
)

__all__ = ["HandlerViewStatusLineReceiptEffect", "ModelSeamEmitterRequest"]

NODE_NAME = "node_view_status_line_receipt_effect"


class HandlerViewStatusLineReceiptEffect:
    def handle(self, request: ModelSeamEmitterRequest) -> ModelSeamEmitterInsufficient:
        return ModelSeamEmitterInsufficient(
            emitter=NODE_NAME,
            seam_id=request.seam_id,
            side=request.side,
            reason="status line consumer's receipt is not built yet: it emits no count or window close for VS-14 receiver",
        )
