# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the one reader of a contract's declared topics."""

from pathlib import Path

from omnimarket.models.model_contract_topics import (
    ModelContractTopicReport,
    ModelContractTopicRequest,
)

from .handlers.handler_contract_topic_reader import HandlerContractTopicReader


class NodeContractTopicReaderCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerContractTopicReader()

    def handle(self, request: ModelContractTopicRequest) -> ModelContractTopicReport:
        return self.handler.handle(request)
