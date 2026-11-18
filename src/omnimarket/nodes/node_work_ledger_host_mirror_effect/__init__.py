# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep a lab host's copy of the rolling work ledger within minutes of the ledger of record (OMN-25537)."""

from omnimarket.nodes.node_work_ledger_host_mirror_effect.handlers.handler_work_ledger_host_mirror import (
    HandlerWorkLedgerHostMirror,
)
from omnimarket.nodes.node_work_ledger_host_mirror_effect.models.model_work_ledger_host_mirror import (
    EnumHostMirrorStatus,
    ModelHostMirrorRequest,
    ModelHostMirrorResult,
)


class NodeWorkLedgerHostMirrorEffect(HandlerWorkLedgerHostMirror):
    """ONEX entry-point wrapper for HandlerWorkLedgerHostMirror."""


__all__ = [
    "EnumHostMirrorStatus",
    "HandlerWorkLedgerHostMirror",
    "ModelHostMirrorRequest",
    "ModelHostMirrorResult",
    "NodeWorkLedgerHostMirrorEffect",
]
