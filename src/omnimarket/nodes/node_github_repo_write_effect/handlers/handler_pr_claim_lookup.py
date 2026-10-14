# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live claim lookup: node_pr_claim_registry_effect's own operations.

pr_close is refused unless the requesting lane holds the PR's claim. Whether a
claim is live, and what it says, is the registry's decision: this lookup runs
the registry's ``has_active`` and ``get_claim`` operations through its handler,
with the registry's models (promoted to ``omnimarket.models.pr_claim``), against
the claims directory it is given, exactly as the registry takes it.

A caller's claim is written to the caller's own registry, which the effect
cannot read. The caller carries it on the command (``ModelRepoWriteClaim``);
``carried_claim_refusal`` judges it with the registry's own liveness rule.
"""

from __future__ import annotations

from datetime import timedelta

from omnimarket.models.pr_claim import (
    EnumPrClaimOperation,
    ModelPrClaim,
    ModelPrClaimRegistryRequest,
)
from omnimarket.nodes.node_github_repo_write_effect.models.model_repo_write_io import (
    ModelRepoWriteClaim,
    ModelRepoWriteRequest,
    parse_claim_stamp,
)
from omnimarket.nodes.node_pr_claim_registry_effect.handlers.handler_pr_claim_registry import (
    HandlerPrClaimRegistry,
    is_active,
)

# A carried stamp this far ahead of the effect's clock is clock skew; further
# ahead, it would keep an expired claim live.
_CLOCK_SKEW = timedelta(minutes=5)


def registry_claim_refusal(
    claim: ModelPrClaim, request: ModelRepoWriteRequest
) -> str | None:
    """Why a live registry claim is not the requester's; None when it is."""
    if claim.claimed_by_run != request.requested_by_run or (
        claim.lane_id and claim.lane_id != request.requested_by_lane
    ):
        return (
            f"{request.pr_key} is claimed by run {claim.claimed_by_run} lane "
            f"{claim.lane_id}, not run {request.requested_by_run} lane "
            f"{request.requested_by_lane}"
        )
    return None


def carried_claim_refusal(
    claim: ModelRepoWriteClaim, request: ModelRepoWriteRequest, now: str
) -> str | None:
    """Why a claim carried on a pr_close is not the requester's live claim.

    None when it names this PR, this lane and this run, is not dated ahead of
    ``now`` by more than clock skew, and the claim registry's liveness rule
    (``is_active``) holds at ``now``.
    """
    if claim.pr_key.lower() != request.pr_key:
        return f"the carried claim is on {claim.pr_key}, not {request.pr_key}"
    if claim.claimed_by_run != request.requested_by_run or (
        claim.lane_id != request.requested_by_lane
    ):
        return (
            f"{request.pr_key} is claimed by run {claim.claimed_by_run} lane "
            f"{claim.lane_id}, not run {request.requested_by_run} lane "
            f"{request.requested_by_lane}"
        )
    if claim.latest_stamp > parse_claim_stamp(now) + _CLOCK_SKEW:
        return (
            f"the carried claim on {request.pr_key} is dated in the future "
            f"({claim.latest_stamp:%Y-%m-%dT%H:%M:%SZ}, now {now})"
        )
    if not is_active(
        {
            "claimed_at": claim.claimed_at,
            "last_heartbeat_at": claim.last_heartbeat_at or claim.claimed_at,
        },
        now,
    ):
        return (
            f"the carried claim on {request.pr_key} expired (taken "
            f"{claim.claimed_at}, last heartbeat {claim.last_heartbeat_at}, now {now})"
        )
    return None


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

        if self._registry.handle(ask(EnumPrClaimOperation.HAS_ACTIVE)).succeeded:
            return self._registry.handle(ask(EnumPrClaimOperation.GET_CLAIM)).claim
        # pr_claim_registry_cli.py files a claim under the key as typed, so a
        # claim on OmniNode-ai/x#1 sits in OmniNode-ai--x--1.json while the
        # canonical key asked for is lowercase. Match the live claims by key.
        wanted = pr_key.lower()
        live = self._registry.handle(ask(EnumPrClaimOperation.LIST_ACTIVE)).claims
        return next(
            (claim for claim in live if (claim.pr_key or "").lower() == wanted), None
        )


__all__: list[str] = [
    "HandlerPrClaimLookup",
    "carried_claim_refusal",
    "registry_claim_refusal",
]
