# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR landing ledger facts compute: the ledger facts of one landing tick, from parsed ledger rows.

``HandlerPrLandingLedgerFacts.handle(ModelLandingLedgerRows) -> ModelLandingLedgerFacts``
(definition-B). It derives the fixer kill switch, the owners of shared red causes, the declared
cause fixes, the cause releases, the lab proof PASS readbacks and the live drain lane.
"""

from omnimarket.nodes.node_pr_landing_ledger_facts_compute.handlers.handler_pr_landing_ledger_facts import (
    HandlerPrLandingLedgerFacts,
    derive_ledger_facts,
)


class NodePrLandingLedgerFactsCompute(HandlerPrLandingLedgerFacts):
    """ONEX entry-point wrapper for HandlerPrLandingLedgerFacts."""


__all__ = [
    "HandlerPrLandingLedgerFacts",
    "NodePrLandingLedgerFactsCompute",
    "derive_ledger_facts",
]
