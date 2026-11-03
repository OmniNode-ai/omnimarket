# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative shared host state node."""

from pathlib import Path

from omnimarket.handlers.handler_host_shell import HandlerHostShell


class NodeHostStateEffect(HandlerHostShell):
    """Read-only host measurement through a profile-configured shell port."""

    contract_path = Path(__file__).with_name("contract.yaml")
