# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-driven compute shell; behavior belongs to the handler."""

from omnibase_core.nodes.node_compute import NodeCompute

from .models.model_ledger_rows_build import (
    ModelLedgerRowsBuildRequest,
    ModelLedgerRowsBuildResult,
)


class NodeLedgerRowsBuildCompute(
    NodeCompute[ModelLedgerRowsBuildRequest, ModelLedgerRowsBuildResult]
):
    """Builds ledger row argv from typed fields or a supplied JSON request object, returning refusals without running a writer."""
