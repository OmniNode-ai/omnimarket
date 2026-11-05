# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative effect node with an injectable preservation handler."""

from pathlib import Path

from .handler import HandlerInstallOnlyPreserve
from .models import (
    ModelInstallOnlyOverlay,
    ModelInstallOnlyRequest,
    ModelInstallOnlyResult,
)


class NodeInstallOnlyPreserveEffect:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(
        self,
        *,
        overlay: ModelInstallOnlyOverlay | None = None,
        handler: HandlerInstallOnlyPreserve | None = None,
    ) -> None:
        self.handler = (
            handler
            if handler is not None
            else HandlerInstallOnlyPreserve(overlay=overlay)
        )

    def handle(self, request: ModelInstallOnlyRequest) -> ModelInstallOnlyResult:
        return self.handler.handle(request)
