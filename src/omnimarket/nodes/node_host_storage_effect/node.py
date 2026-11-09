# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for node_host_storage_effect."""

from pathlib import Path

from .handlers.handler_host_storage import HandlerHostStorage


class NodeHostStorageEffect(HandlerHostStorage):
    """Contract-defined host storage node."""

    contract_path = Path(__file__).with_name("contract.yaml")
