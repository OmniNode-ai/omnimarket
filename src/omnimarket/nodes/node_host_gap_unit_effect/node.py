# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for node_host_gap_unit_effect."""

from pathlib import Path

from .handlers.handler_host_gap_unit import HandlerHostGapUnit


class NodeHostGapUnitEffect(HandlerHostGapUnit):
    """Contract-defined host declaration node."""

    contract_path = Path(__file__).with_name("contract.yaml")
