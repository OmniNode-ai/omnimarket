# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the remote-lane runner's pre-placement brief and lane-shape refusals."""

from pathlib import Path

from .handlers.handler_dispatch_brief_shape import HandlerDispatchBriefShape
from .models.model_dispatch_brief_shape import (
    ModelDispatchBriefShapeRequest,
    ModelDispatchBriefShapeResult,
)


class NodeDispatchBriefShapeCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerDispatchBriefShape()

    def handle(
        self, request: ModelDispatchBriefShapeRequest
    ) -> ModelDispatchBriefShapeResult:
        return self.handler.handle(request)
