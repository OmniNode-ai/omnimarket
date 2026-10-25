# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the lab host parity probe's capability half."""

from pathlib import Path

from .handlers.handler_host_capability import HandlerHostCapability


class NodeHostCapabilityCompute(HandlerHostCapability):
    """Declarative entry point for host capability operations."""

    contract_path = Path(__file__).with_name("contract.yaml")
