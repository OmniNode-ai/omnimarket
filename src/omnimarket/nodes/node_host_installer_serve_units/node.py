# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the lab-work serve unit installer."""

from pathlib import Path

from .handlers.handler_serve_units import HandlerServeUnits


class NodeHostInstallerServeUnits(HandlerServeUnits):
    """Declarative entry point for the serve unit installer."""

    contract_path = Path(__file__).with_name("contract.yaml")
