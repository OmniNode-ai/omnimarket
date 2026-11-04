# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeLedgerReconcileCommandCompute - contract-driven; all behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.nodes.node_ledger_reconcile_command_compute.models.model_ledger_reconcile_command import (
    ModelLedgerReconcileCommandRequest,
    ModelLedgerReconcileCommandResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeLedgerReconcileCommandCompute(
    NodeCompute[ModelLedgerReconcileCommandRequest, ModelLedgerReconcileCommandResult]
):
    """Compute node: Decide the ledger reconcile command and cache environment from caller-supplied resolver and executable facts."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeLedgerReconcileCommandCompute"]
