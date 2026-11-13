# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the host table keys read effect."""

from pathlib import Path

from .handlers.handler_host_table_keys_read import HandlerHostTableKeysRead


class NodeHostTableKeysReadEffect(HandlerHostTableKeysRead):
    """Declarative entry point for the host table keys read effect."""

    contract_path = Path(__file__).with_name("contract.yaml")
