# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the dispatch's repository grant."""

from pathlib import Path

from .handlers.handler_dispatch_repo_grant import HandlerDispatchRepoGrant
from .models.model_dispatch_repo_grant import (
    ModelDispatchRepoGrantRequest,
    ModelDispatchRepoGrantResult,
)


class NodeDispatchRepoGrantCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerDispatchRepoGrant()

    def handle(
        self, request: ModelDispatchRepoGrantRequest
    ) -> ModelDispatchRepoGrantResult:
        return self.handler.handle(request)
