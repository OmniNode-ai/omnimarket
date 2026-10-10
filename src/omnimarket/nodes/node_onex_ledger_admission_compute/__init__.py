# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""onex-ledger admission compute node (OMN-20686)."""

from omnimarket.nodes.node_onex_ledger_admission_compute.handlers.handler_onex_ledger_admission import (
    HandlerOnexLedgerAdmission,
)


class NodeOnexLedgerAdmissionCompute(HandlerOnexLedgerAdmission):
    """ONEX entrypoint for what the onex-ledger wrapper does with an argv."""


__all__ = ["HandlerOnexLedgerAdmission", "NodeOnexLedgerAdmissionCompute"]
