# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative pure rule matcher."""

from pathlib import Path

from .handler import HandlerErrorRuleMatch


class NodeErrorRuleMatchCompute(HandlerErrorRuleMatch):
    contract_path = Path(__file__).with_name("contract.yaml")
