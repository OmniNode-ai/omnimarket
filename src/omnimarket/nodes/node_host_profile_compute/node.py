# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for node_host_profile_compute."""

from pathlib import Path

from omnimarket.handlers.handler_host_profile import HandlerHostProfileCompute


class NodeHostProfileCompute(HandlerHostProfileCompute):
    """Contract-defined host declaration node."""

    contract_path = Path(__file__).with_name("contract.yaml")
