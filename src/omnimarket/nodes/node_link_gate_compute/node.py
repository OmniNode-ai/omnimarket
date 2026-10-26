# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the ssh link gate."""

from pathlib import Path

from .handlers.handler_link_gate import HandlerLinkGate
from .models.model_link_gate import ModelLinkGateRequest as ModelLinkGateRequest
from .models.model_link_gate import ModelLinkGateVerdict as ModelLinkGateVerdict


class NodeLinkGateCompute(HandlerLinkGate):
    """Declarative entry point for link gate operations."""

    contract_path = Path(__file__).with_name("contract.yaml")
