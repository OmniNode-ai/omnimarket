# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry of the review lock node: the contract path and its two handlers."""

from __future__ import annotations

from pathlib import Path

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeReviewLockEffect:
    """Takes the review lock of a pull request head and checks a fixer push's scope."""

    contract_path = CONTRACT
