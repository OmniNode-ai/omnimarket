# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical local entry point for the Codex model choice."""

from .handlers.handler_codex_model import CONTRACT_PATH, HandlerCodexModel


class NodeCodexModelCompute(HandlerCodexModel):
    contract_path = CONTRACT_PATH
