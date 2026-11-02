# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the launch-time validity check of the remote-lane runner."""

from pathlib import Path

from .handlers.handler_launch_validity import HandlerLaunchValidity
from .models.model_launch_validity import (
    ModelLaunchValidityRequest,
    ModelLaunchValidityResult,
)


class NodeLaunchValidityCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerLaunchValidity()

    def handle(self, request: ModelLaunchValidityRequest) -> ModelLaunchValidityResult:
        return self.handler.handle(request)
