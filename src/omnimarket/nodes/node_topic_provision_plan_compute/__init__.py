# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the broker topic provision plan."""

from pathlib import Path

from .handlers.handler_topic_provision_plan import HandlerTopicProvisionPlan
from .models.model_topic_provision_plan import (
    ModelTopicProvisionPlanRequest,
    ModelTopicProvisionPlanResult,
)


class NodeTopicProvisionPlanCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerTopicProvisionPlan()

    def handle(
        self, request: ModelTopicProvisionPlanRequest
    ) -> ModelTopicProvisionPlanResult:
        return self.handler.handle(request)
