# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""What the routing ladder tried, and why each rung was not it (OMN-18427).

The routing authority raises when no tier yields a decision. Before this
module it raised a single sentence that named no candidate, no reason, and no
contract — and offered a remedy (an overlay file, an environment variable)
that is not the configuration surface of a deployed lane at all. On onex-dev
that sentence was untrue three ways at once: the endpoints WERE configured,
the overlay file it named does not exist on the image, and the real exclusion
was credential resolution.

These models carry the facts the selector already computed. Every field is
typed and factual — a declared reference NAME, a quota domain, a token count.
No free-text reason, and never a secret VALUE: the point of naming
``llm.glm.api_key`` is to tell an operator which declaration to look at, and
that is served by the name alone.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_routing_exclusion import EnumRoutingExclusionReason

# The contract surfaces a route is declared on. Named as committed repository
# paths because that is what an operator edits; deliberately NOT the
# environment variables that happen to point a given lane at them, which is
# the substitution this ticket exists to remove.
BIFROST_CONTRACT_SURFACE: str = "src/omnimarket/configs/bifrost_delegation.yaml"
ROUTING_TIERS_SURFACE: str = "src/omnimarket/configs/routing_tiers.yaml"
TASK_CLASS_CONTRACT_SURFACE: str = "src/omnimarket/configs/task_class_contracts.v1.yaml"
LANE_OVERLAY_SURFACE: str = "the lane's typed Bifrost overlay (bifrost_lane_overlay)"
SECRET_RESOLVER_SURFACE: str = "the runtime secret-resolver configuration"


class ModelRoutingCandidateExclusion(BaseModel):
    """One rung the ladder considered, and the reason it was not taken."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier_name: str = Field(..., description="Routing tier the candidate sits in.")
    reason: EnumRoutingExclusionReason = Field(
        ..., description="Which rejection branch excluded this candidate."
    )
    backend_ref: str | None = Field(
        default=None,
        description=(
            "Backend the tier names. None on a tier-level row, where the tier "
            "was skipped before any of its models was considered."
        ),
    )
    model_id: str | None = Field(
        default=None, description="Model id the tier declares for that backend."
    )
    declared_secret_ref: str | None = Field(
        default=None,
        description=(
            "The credential reference NAME the bifrost contract declares for "
            "this backend. Never a value — the name is what an operator needs "
            "in order to find the declaration that is unmapped."
        ),
    )
    quota_domain: str | None = Field(
        default=None, description="Provider quota domain currently barred, if any."
    )
    quota_lifts_at: datetime | None = Field(
        default=None, description="When the provider said the quota domain returns."
    )
    estimated_tokens: int | None = Field(
        default=None, description="Estimated prompt size, when size is the exclusion."
    )
    max_context_tokens: int | None = Field(
        default=None, description="The candidate's declared context ceiling."
    )

    def render(self) -> str:
        """One line: what was tried, and the fact that ruled it out."""
        target = self.backend_ref or "(tier)"
        if self.model_id is not None:
            target = f"{target} / {self.model_id}"
        line = f"  - tier '{self.tier_name}' {target}: {self.reason.value}"
        if self.declared_secret_ref is not None:
            line += f" (declared secret_ref '{self.declared_secret_ref}')"
        if self.quota_domain is not None:
            line += f" (quota domain '{self.quota_domain}'"
            if self.quota_lifts_at is not None:
                line += f", lifts at {self.quota_lifts_at.isoformat()}"
            line += ")"
        if self.estimated_tokens is not None and self.max_context_tokens is not None:
            line += (
                f" (estimated {self.estimated_tokens} tokens "
                f"> ceiling {self.max_context_tokens})"
            )
        return line


class ModelRoutingExclusionReport(BaseModel):
    """Every rung the ladder walked for one request, in the order it walked it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="The request being routed.")
    task_type: str = Field(..., description="Task class whose ladder was resolved.")
    tier_order: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Tier names in the contract-resolved escalation order.",
    )
    candidates: tuple[ModelRoutingCandidateExclusion, ...] = Field(
        default_factory=tuple,
        description="Excluded candidates, in walk order.",
    )
    routable_candidate_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Candidates this report could find NO exclusion for. Normally zero "
            "when the selector returned nothing. A non-zero value means the "
            "report and the selector disagree, and the rendered message says "
            "so rather than inventing a reason."
        ),
    )

    def render(self) -> str:
        """The refusal body: what was tried, why not, and what declares it."""
        head = (
            f"No routable backend for task_type='{self.task_type}'. "
            f"The contract-resolved tier order was "
            f"[{', '.join(self.tier_order) or 'none'}] and every candidate in it "
            f"was excluded:"
        )
        body = "\n".join(candidate.render() for candidate in self.candidates) or (
            "  - (the resolved tier order declared no candidates at all)"
        )

        surfaces = [
            f"A route is declared by the backend entry in {BIFROST_CONTRACT_SURFACE} "
            f"(endpoint_url, model_name, secret_ref), as the lane renders it "
            f"through {LANE_OVERLAY_SURFACE}; by the tier entry in "
            f"{ROUTING_TIERS_SURFACE} (backend_id, use_for, max_context_tokens); "
            f"and by the class entry in {TASK_CLASS_CONTRACT_SURFACE} "
            f"(escalation_policy.tier_order, cloud_routing_policy, "
            f"pricing_ceiling_per_1k_tokens)."
        ]
        if any(
            candidate.reason is EnumRoutingExclusionReason.BACKEND_SECRET_REF_UNRESOLVED
            for candidate in self.candidates
        ):
            surfaces.append(
                f"A rung excluded on credential resolution is one whose declared "
                f"secret_ref {SECRET_RESOLVER_SURFACE} does not map. On a lane "
                f"whose platform ladder is deliberately credential-less, that is "
                f"the expected outcome for every house-credentialed rung, and the "
                f"delegation needs a tenant-scoped route rather than a new "
                f"platform credential."
            )
        if self.routable_candidate_count:
            surfaces.append(
                f"{self.routable_candidate_count} candidate(s) passed every "
                f"exclusion check this report applies, yet selection still "
                f"returned nothing — the reported reasons are incomplete for "
                f"this request."
            )

        return "\n".join([head, body, *surfaces])


__all__: list[str] = [
    "BIFROST_CONTRACT_SURFACE",
    "LANE_OVERLAY_SURFACE",
    "ROUTING_TIERS_SURFACE",
    "SECRET_RESOLVER_SURFACE",
    "TASK_CLASS_CONTRACT_SURFACE",
    "ModelRoutingCandidateExclusion",
    "ModelRoutingExclusionReport",
]
