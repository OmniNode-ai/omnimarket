# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the link read history."""

from pathlib import Path

from .handlers.handler_link_history import HandlerLinkHistory


class NodeLinkHistoryEffect(HandlerLinkHistory):
    """Declarative entry point for link history operations."""

    contract_path = Path(__file__).with_name("contract.yaml")
