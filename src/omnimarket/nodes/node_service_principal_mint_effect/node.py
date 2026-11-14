# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for service principal minting."""

from pathlib import Path

from .handlers.handler_service_principal_mint import HandlerServicePrincipalMint


class NodeServicePrincipalMintEffect(HandlerServicePrincipalMint):
    """Declarative entry point; the broker, vault and delivery boundaries are injected."""

    contract_path = Path(__file__).with_name("contract.yaml")
