# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for a queued lane's relaunch command."""

from pathlib import Path

from .handlers.handler_lane_queue_argv import HandlerLaneQueueArgv
from .models.model_lane_queue_argv import (
    ModelLaneQueueArgvRequest,
    ModelLaneQueueArgvResult,
)


class NodeLaneQueueArgvCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerLaneQueueArgv()

    def handle(self, request: ModelLaneQueueArgvRequest) -> ModelLaneQueueArgvResult:
        return self.handler.handle(request)
