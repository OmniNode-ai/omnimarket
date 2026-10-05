# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure fold over one lab job transition (OMN-20604 M2).

Rule 7a definition-B: ``handle(request) -> result``. No clock, no broker, no
database. Every time the rows carry is a field of the event, so the same event
always folds to the same rows and a replay is byte-identical.

This class is half of the pair. The writer beside it is the entry the runtime
calls; a pure entry alone on a projection arm validates, returns and stores
nothing while every watermark reads healthy (rule 7a, OMN-18769).
"""

from __future__ import annotations

from omnimarket.nodes.node_projection_lab_job.models import (
    ModelLabJobProjectionRequest,
    ModelLabJobProjectionResult,
)


class HandlerProjectionLabJob:
    """Folds one transitioned event into the complete state and transition rows."""

    def handle(
        self, request: ModelLabJobProjectionRequest
    ) -> ModelLabJobProjectionResult:
        event = request.transitioned
        return ModelLabJobProjectionResult(
            state_row=event.row.model_dump(mode="python"),
            transition_row=event.transition.model_dump(mode="python"),
        )


__all__ = ["HandlerProjectionLabJob"]
