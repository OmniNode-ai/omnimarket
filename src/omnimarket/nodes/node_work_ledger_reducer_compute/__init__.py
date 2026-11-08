# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Work-ledger reducer compute node: the shadow judge, the guard replay and the accepted-row rebuild."""

from omnimarket.nodes.node_work_ledger_reducer_compute.handlers.handler_work_ledger_reducer import (
    HandlerWorkLedgerReducer,
)


class NodeWorkLedgerReducerCompute(HandlerWorkLedgerReducer):
    """ONEX entrypoint for the work-ledger reducer's shadow judge, guard replay and rebuild."""


__all__ = ["HandlerWorkLedgerReducer", "NodeWorkLedgerReducerCompute"]
