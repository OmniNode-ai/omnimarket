# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed local boundary around the hook's access-mode detector."""

from omnimarket.models.model_local_access_mode_request import (
    ModelLocalAccessModeRequest,
)
from omnimarket.models.model_local_access_mode_result import ModelLocalAccessModeResult
from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_access_mode_detect import (
    HandlerAccessModeDetect,
)
from omnimarket.nodes.node_operator_access_mode_effect.models.model_access_mode_request import (
    ModelAccessModeRequest,
)


class HandlerLocalAccessMode:
    """Expose the detector without changing its existing dataclass callers."""

    def handle(
        self, request: ModelLocalAccessModeRequest
    ) -> ModelLocalAccessModeResult:
        result = HandlerAccessModeDetect().handle(
            ModelAccessModeRequest(
                session_id=request.session_id,
                state_dir=request.state_dir,
                environ=request.environ,
                transcript_entrypoint=request.transcript_entrypoint,
            )
        )
        return ModelLocalAccessModeResult(
            mode=result.mode,
            source=result.source,
            detected=result.detected,
            signals=result.signals,
        )
