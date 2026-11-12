# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the required-and-absent topic reading."""

from pathlib import Path

from omnimarket.models.model_topic_required_absent import (
    ModelRequiredAbsentReport,
    ModelRequiredAbsentRequest,
)

from .handlers.handler_topic_required_absent import HandlerTopicRequiredAbsent


class NodeTopicRequiredAbsentCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerTopicRequiredAbsent()

    def handle(self, request: ModelRequiredAbsentRequest) -> ModelRequiredAbsentReport:
        return self.handler.handle(request)
