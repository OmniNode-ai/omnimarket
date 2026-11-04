# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative error-rule replay node."""

from pathlib import Path

from .handler import HandlerErrorRuleReplay


class NodeErrorRuleReplayCompute(HandlerErrorRuleReplay):
    contract_path = Path(__file__).with_name("contract.yaml")
