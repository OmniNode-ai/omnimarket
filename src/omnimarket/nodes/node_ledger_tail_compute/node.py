# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-driven compute shell; behavior belongs to the handler."""

from omnibase_core.nodes.node_compute import NodeCompute

from .models.model_ledger_tail import ModelLedgerTailRequest, ModelLedgerTailResult


class NodeLedgerTailCompute(NodeCompute[ModelLedgerTailRequest, ModelLedgerTailResult]):
    """Parses supplied ledger text and decides exact PR ownership, live drains and failed attempts from caller-supplied facts."""
