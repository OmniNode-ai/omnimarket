# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative NO-MATCH exception effect."""

from pathlib import Path

from .handler import HandlerErrorRuleNoMatch


class NodeErrorRuleNoMatchEffect(HandlerErrorRuleNoMatch):
    contract_path = Path(__file__).with_name("contract.yaml")
