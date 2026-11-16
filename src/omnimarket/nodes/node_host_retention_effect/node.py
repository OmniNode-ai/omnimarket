# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the host retention node."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
    HandlerHostRetention,
)


class NodeHostRetentionEffect(HandlerHostRetention):
    """Declarative entry point for the contract-bound host retention handler."""

    contract_path = Path(__file__).with_name("contract.yaml")
