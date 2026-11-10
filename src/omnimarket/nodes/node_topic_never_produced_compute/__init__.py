# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the never-produced topic listing."""

from pathlib import Path

from .handlers.handler_topic_never_produced import HandlerTopicNeverProduced
from .models.model_topic_never_produced import (
    ModelNeverProducedReport,
    ModelNeverProducedRequest,
)


class NodeTopicNeverProducedCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerTopicNeverProduced()

    def handle(self, request: ModelNeverProducedRequest) -> ModelNeverProducedReport:
        return self.handler.handle(request)
