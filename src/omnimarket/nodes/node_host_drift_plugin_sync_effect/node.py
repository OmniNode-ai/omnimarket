# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for node_host_drift_plugin_sync_effect."""

from pathlib import Path

from .handlers.handler_plugin_sync import HandlerHostDriftPluginSync


class NodeHostDriftPluginSyncEffect(HandlerHostDriftPluginSync):
    """Contract-defined data-plugin revision sync."""

    contract_path = Path(__file__).with_name("contract.yaml")
