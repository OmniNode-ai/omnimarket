# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for node_host_drift_plugin_rules_effect."""

from pathlib import Path

from .handlers.handler_plugin_rules import HandlerHostDriftPluginRules


class NodeHostDriftPluginRulesEffect(HandlerHostDriftPluginRules):
    """Contract-defined plugin sync rules overlay load."""

    contract_path = Path(__file__).with_name("contract.yaml")
