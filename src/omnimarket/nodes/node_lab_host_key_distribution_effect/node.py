# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for lab host key distribution."""

from __future__ import annotations

from pathlib import Path

from .handlers.handler_lab_host_key_distribution import HandlerLabHostKeyDistribution


class NodeLabHostKeyDistributionEffect(HandlerLabHostKeyDistribution):
    """Declarative entry point for distributing public keys among lab hosts."""

    contract_path = Path(__file__).with_name("contract.yaml")
