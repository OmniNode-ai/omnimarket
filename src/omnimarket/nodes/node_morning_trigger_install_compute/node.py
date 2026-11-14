# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for node_morning_trigger_install_compute."""

from pathlib import Path

from .handlers.handler_morning_trigger_install import HandlerMorningTriggerInstall


class NodeMorningTriggerInstallCompute(HandlerMorningTriggerInstall):
    """Contract-defined morning installer copy plan."""

    contract_path = Path(__file__).with_name("contract.yaml")
