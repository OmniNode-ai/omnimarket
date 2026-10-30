# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the duplicate-fix pull request guard."""

from pathlib import Path

from .handlers.handler_pr_duplicate_guard import HandlerPrDuplicateGuard


class NodePrDuplicateGuardCompute(HandlerPrDuplicateGuard):
    """Declarative entry point for the duplicate-fix pull request guard."""

    contract_path = Path(__file__).with_name("contract.yaml")
