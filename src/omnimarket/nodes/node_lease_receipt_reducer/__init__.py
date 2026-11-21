# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lease receipt reducer: the single writer of the lease tables.

``HandlerLeaseReceiptReducer.handle(ModelLeaseReduceInput) ->
ModelLeaseReduceOutput``. The reducer holds the authoritative table of each kind
behind an injected store (in memory by default); a command must carry exactly the
held table, a recorded write is stored on the version read, the command's ``now``
is bounded by the reducer's clock, and every refusal carries a typed reason.
"""

from omnimarket.nodes.node_lease_receipt_reducer.handlers.handler_lease_receipt_reducer import (
    HandlerLeaseReceiptReducer,
    reduce_lease,
)


class NodeLeaseReceiptReducer(HandlerLeaseReceiptReducer):
    """ONEX entry-point wrapper for HandlerLeaseReceiptReducer."""


__all__ = [
    "HandlerLeaseReceiptReducer",
    "NodeLeaseReceiptReducer",
    "reduce_lease",
]
