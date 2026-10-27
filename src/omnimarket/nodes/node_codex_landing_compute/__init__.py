# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for deterministic Codex landing command preparation."""

from pathlib import Path

from .handlers.handler_codex_landing import HandlerCodexLanding
from .models.model_codex_landing import (
    ModelCodexLandingRequest,
    ModelCodexLandingResult,
)


class NodeCodexLandingCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerCodexLanding()

    def handle(self, request: ModelCodexLandingRequest) -> ModelCodexLandingResult:
        return self.handler.handle(request)
