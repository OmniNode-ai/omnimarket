# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the duplicate-fix guard's snapshot reader."""

from pathlib import Path

from .handlers.handler_pr_duplicate_snapshot import HandlerPrDuplicateSnapshot


class NodePrDuplicateSnapshotEffect(HandlerPrDuplicateSnapshot):
    """Declarative entry point for the duplicate-fix guard's snapshot reader."""

    contract_path = Path(__file__).with_name("contract.yaml")
