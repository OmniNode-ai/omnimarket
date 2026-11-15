# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the canonical clone sync unit installer."""

from pathlib import Path

from .handlers.handler_clone_sync_units import HandlerCloneSyncUnits


class NodeHostInstallerCloneSyncUnits(HandlerCloneSyncUnits):
    """Declarative entry point for the canonical clone sync unit installer."""

    contract_path = Path(__file__).with_name("contract.yaml")
