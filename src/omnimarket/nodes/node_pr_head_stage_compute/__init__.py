# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for a remote lane's PR-head fetch and stage shell."""

from pathlib import Path

from .handlers.handler_pr_head_stage import HandlerPrHeadStage
from .models.model_pr_head_stage import ModelPrHeadStageRequest, ModelPrHeadStageResult


class NodePrHeadStageCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerPrHeadStage()

    def handle(self, request: ModelPrHeadStageRequest) -> ModelPrHeadStageResult:
        return self.handler.handle(request)
