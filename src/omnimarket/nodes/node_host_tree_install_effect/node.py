# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the host tree installer."""

from pathlib import Path

from .handlers.handler_tree_install import HandlerTreeInstall


class NodeHostTreeInstallEffect(HandlerTreeInstall):
    """Expose the contract handler without adding node-layer execution logic."""

    contract_path = Path(__file__).with_name("contract.yaml")
