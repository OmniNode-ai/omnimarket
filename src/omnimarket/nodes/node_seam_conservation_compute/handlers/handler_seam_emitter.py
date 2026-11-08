# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared seam emitter until the measurement operation is installed."""

from omnimarket.models.seam_reading.model_seam_emitter import (
    ModelSeamEmitterInsufficient,
    ModelSeamEmitterRequest,
)


class HandlerSeamEmitter:
    def handle(self, request: ModelSeamEmitterRequest) -> ModelSeamEmitterInsufficient:
        return ModelSeamEmitterInsufficient(
            emitter="node_seam_conservation_compute",
            seam_id=request.seam_id,
            side=request.side,
            reason="seam measurement not built",
        )
