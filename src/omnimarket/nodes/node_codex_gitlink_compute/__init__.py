# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the Codex lane's worktree ``.git`` link lines."""

from pathlib import Path

from .handlers.handler_codex_gitlink import HandlerCodexGitlink
from .models.model_codex_gitlink import (
    ModelCodexGitlinkRequest,
    ModelCodexGitlinkResult,
)


class NodeCodexGitlinkCompute:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerCodexGitlink()

    def handle(self, request: ModelCodexGitlinkRequest) -> ModelCodexGitlinkResult:
        return self.handler.handle(request)
