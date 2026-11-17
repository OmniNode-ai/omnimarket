# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for node_host_drift_plugin_revision_orchestrator."""

from pathlib import Path

from .handlers.handler_plugin_revision_check import HandlerHostDriftPluginRevisionCheck


class NodeHostDriftPluginRevisionOrchestrator(HandlerHostDriftPluginRevisionCheck):
    """Contract-defined host-drift plugin revision check."""

    contract_path = Path(__file__).with_name("contract.yaml")
