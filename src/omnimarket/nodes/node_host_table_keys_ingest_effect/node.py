# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the host table keys ingest effect."""

from pathlib import Path

from .handlers.handler_host_table_keys_ingest import HandlerHostTableKeysIngest


class NodeHostTableKeysIngestEffect(HandlerHostTableKeysIngest):
    """Declarative entry point for the host table keys ingest effect."""

    contract_path = Path(__file__).with_name("contract.yaml")
