# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for node_host_drift_check_effect."""

from pathlib import Path

from .handlers.handler_host_drift_check import HandlerHostDriftCheck


class NodeHostDriftCheckEffect(HandlerHostDriftCheck):
    """Contract-defined host declaration node."""

    contract_path = Path(__file__).with_name("contract.yaml")
