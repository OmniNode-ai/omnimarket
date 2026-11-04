# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative answer effect entry point."""

from pathlib import Path

from .handlers.handler_operator_answer import HandlerOperatorAnswer


class NodeOperatorAnswerEffect(HandlerOperatorAnswer):
    contract_path = Path(__file__).with_name("contract.yaml")
