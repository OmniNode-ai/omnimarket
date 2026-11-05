# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry; the contract-selected daily handler owns the effect."""

from pathlib import Path

from .handlers import HandlerErrorRuleMeasurementDaily


class NodeErrorRuleMeasurementEffect(HandlerErrorRuleMeasurementDaily):
    """Daily error-rule measurements through the contract's injected handler."""

    contract_path = Path(__file__).with_name("contract.yaml")
