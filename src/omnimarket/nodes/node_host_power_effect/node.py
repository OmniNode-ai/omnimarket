# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for node_host_power_effect."""

from pathlib import Path

from .handlers.handler_host_power import HandlerHostPower


class NodeHostPowerEffect(HandlerHostPower):
    """Contract-defined host power node."""

    contract_path = Path(__file__).with_name("contract.yaml")
