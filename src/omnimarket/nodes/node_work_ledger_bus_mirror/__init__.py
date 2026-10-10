# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab work terminal ledger mapping node (OMN-20278)."""

from omnimarket.models.model_work_ledger_bus_mirror_request import (
    ModelWorkLedgerBusMirrorRequest,
)
from omnimarket.nodes.node_work_ledger_bus_mirror.handlers.handler_work_ledger_bus_mirror import (
    HandlerWorkLedgerBusMirror,
)


class NodeWorkLedgerBusMirror(HandlerWorkLedgerBusMirror):
    """Declarative entrypoint hosted by the ledger serve process."""


__all__ = [
    "HandlerWorkLedgerBusMirror",
    "ModelWorkLedgerBusMirrorRequest",
    "NodeWorkLedgerBusMirror",
]
