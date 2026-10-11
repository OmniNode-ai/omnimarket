# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live claim lookup: node_pr_claim_registry_effect's own operations.

pr_close is refused unless the requesting lane holds the PR's claim. Whether a
claim is live, and what it says, is the registry's decision: this lookup runs
the registry's ``has_active`` and ``get_claim`` operations through its handler,
with the registry's models (promoted to ``omnimarket.models.pr_claim``), against
the claims directory the request names, exactly as the registry takes it.
"""

from __future__ import annotations

from omnimarket.models.pr_claim import (
    EnumPrClaimOperation,
    ModelPrClaim,
    ModelPrClaimRegistryRequest,
)
from omnimarket.nodes.node_pr_claim_registry_effect.handlers.handler_pr_claim_registry import (
    HandlerPrClaimRegistry,
)


class HandlerPrClaimLookup:
    """Satisfies ``ProtocolPrClaimLookup`` with the claim registry's handler."""

    def __init__(self, registry: HandlerPrClaimRegistry | None = None) -> None:
        self._registry = registry or HandlerPrClaimRegistry()

    def active_claim(
        self, *, claims_dir: str, pr_key: str, now: str
    ) -> ModelPrClaim | None:
        def ask(operation: EnumPrClaimOperation) -> ModelPrClaimRegistryRequest:
            return ModelPrClaimRegistryRequest(
                operation=operation, claims_dir=claims_dir, now=now, pr_key=pr_key
            )

        if not self._registry.handle(ask(EnumPrClaimOperation.HAS_ACTIVE)).succeeded:
            return None
        return self._registry.handle(ask(EnumPrClaimOperation.GET_CLAIM)).claim


__all__: list[str] = ["HandlerPrClaimLookup"]
