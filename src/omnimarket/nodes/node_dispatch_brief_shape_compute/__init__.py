# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the remote-lane runner's pre-placement brief and lane-shape refusals."""

from pathlib import Path

from .handlers.handler_dispatch_brief_shape import (
    HandlerBriefDestinations,
    HandlerDispatchBriefShape,
)
from .models.model_dispatch_brief_shape import (
    ModelBriefDestinationRequest,
    ModelBriefDestinationResult,
    ModelDispatchBriefShapeRequest,
    ModelDispatchBriefShapeResult,
)


class NodeDispatchBriefShapeCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerDispatchBriefShape()
        self.destination_handler = HandlerBriefDestinations()

    def handle(
        self, request: ModelDispatchBriefShapeRequest
    ) -> ModelDispatchBriefShapeResult:
        return self.handler.handle(request)

    def check_destinations(
        self, request: ModelBriefDestinationRequest
    ) -> ModelBriefDestinationResult:
        return self.destination_handler.handle(request)
