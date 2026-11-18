# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the lab host table row set."""

from pathlib import Path

from .handlers.handler_host_table import HandlerHostTable


class NodeHostTableCompute(HandlerHostTable):
    """Declarative entry point for host table operations."""

    contract_path = Path(__file__).with_name("contract.yaml")
