# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for a lane brief's host prerequisites."""

from pathlib import Path

from .handlers.handler_brief_prerequisites import HandlerBriefPrerequisites
from .models.model_brief_prerequisites import (
    ModelBriefPrerequisitesRequest,
    ModelBriefPrerequisitesResult,
)


class NodeBriefPrerequisitesCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerBriefPrerequisites()

    def handle(
        self, request: ModelBriefPrerequisitesRequest
    ) -> ModelBriefPrerequisitesResult:
        return self.handler.handle(request)
