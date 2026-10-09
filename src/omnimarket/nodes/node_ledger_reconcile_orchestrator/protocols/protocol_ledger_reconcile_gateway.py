# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How the orchestrator reaches the compute and effect nodes (OMN-20677)."""

from __future__ import annotations

from typing import Protocol

from pydantic import JsonValue

COMPUTE_NODE = "node_ledger_reconcile_compute"
EFFECT_NODE = "node_ledger_reconcile_effect"


class ProtocolLedgerReconcileGateway(Protocol):
    """Sends one operation's request to a node and returns its answer; only JSON passes."""

    async def dispatch(
        self, node: str, operation: str, payload: dict[str, JsonValue]
    ) -> dict[str, JsonValue]: ...
