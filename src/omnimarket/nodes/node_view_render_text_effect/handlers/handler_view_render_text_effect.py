# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""INSUFFICIENT scaffold of the terminal renderer (VS-06 and VS-10 receiver, VS-14 sender).

The seam graph names this node as an emitter before the step that builds it lands, so that every
seam side has a declared producer. Until then it returns a typed INSUFFICIENT result and publishes
nothing; the seams it serves read unreadable, never matched.
"""

from __future__ import annotations

from omnimarket.models.seam_reading import (
    ModelSeamEmitterInsufficient,
    ModelSeamEmitterRequest,
)

__all__ = ["HandlerViewRenderTextEffect", "ModelSeamEmitterRequest"]

NODE_NAME = "node_view_render_text_effect"


class HandlerViewRenderTextEffect:
    def handle(self, request: ModelSeamEmitterRequest) -> ModelSeamEmitterInsufficient:
        return ModelSeamEmitterInsufficient(
            emitter=NODE_NAME,
            seam_id=request.seam_id,
            side=request.side,
            reason="terminal renderer is not built yet: it emits no count or window close for VS-06 and VS-10 receiver, VS-14 sender",
        )
