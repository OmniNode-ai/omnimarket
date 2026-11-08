# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for node_host_drift_plugin_revision_compute."""

from pathlib import Path

from .handlers.handler_plugin_revision import HandlerHostDriftPluginRevision


class NodeHostDriftPluginRevisionCompute(HandlerHostDriftPluginRevision):
    """Contract-defined plugin revision decision."""

    contract_path = Path(__file__).with_name("contract.yaml")
