# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which repositories the audit covers and what each one is expected to carry.

Every list is deployment configuration. The shipped default is empty: the
fleet comes from an overlay (``branch_protection_audit_overlay.v1``) or from
the caller, never from this package, so no deployment's repository names live
in it. An audit over an empty fleet is refused by the entry point rather than
read as compliant.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.nodes.node_branch_protection_audit_effect.models.model_branch_protection_landing_branch import (
    ModelBranchProtectionLandingBranch,
)
from omnimarket.nodes.node_branch_protection_audit_effect.models.model_branch_protection_review_verdict_requirement import (
    ModelBranchProtectionReviewVerdictRequirement,
)

OVERLAY_SCHEMA_VERSION = "branch_protection_audit_overlay.v1"


class ModelBranchProtectionAuditPolicy(BaseModel):
    """The audited fleet and the per-repository roles the checks key on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Repositories audited, in report order.
    repos: tuple[str, ...] = ()
    # Repositories where a "Merge Queue" ruleset is not expected.
    private_repos: tuple[str, ...] = ()
    # Repositories with no protected dev branch: audited on main only.
    dev_exempt_repos: tuple[str, ...] = ()
    # Repositories whose main is advanced only by release automation: empty
    # required contexts plus an update-restricting ruleset with a bypass actor.
    release_synced_main_repos: tuple[str, ...] = ()
    # Repositories audited on dev only.
    main_audit_exempt_repos: tuple[str, ...] = ()
    # Repositories whose main keeps approving and code-owner review enforcement.
    review_gated_main_repos: tuple[str, ...] = ()
    # Repositories that must directly require the "verify / verify" Receipt Gate.
    receipt_gate_required_repos: tuple[str, ...] = ()
    # Repositories whose dev must directly require the "Lane Identity Gate".
    lane_identity_gate_required_repos: tuple[str, ...] = ()
    # Landing branches that must carry an active ruleset with the update rule and
    # exactly the declared bypass actors. A repository not listed here is not
    # judged by this check.
    landing_branches: tuple[ModelBranchProtectionLandingBranch, ...] = ()
    # The required review verdict context's name, supplied by the overlay.
    review_verdict_context: str | None = None
    # (repo, branch) pairs whose landing branch must require that context.
    review_verdict_required: tuple[
        ModelBranchProtectionReviewVerdictRequirement, ...
    ] = ()

    @model_validator(mode="after")
    def _release_synced_and_main_exempt_are_disjoint(
        self,
    ) -> ModelBranchProtectionAuditPolicy:
        both = sorted(
            set(self.release_synced_main_repos) & set(self.main_audit_exempt_repos)
        )
        if both:
            raise ValueError(
                "a repository cannot be both release-synced on main and exempt "
                f"from the main audit: {', '.join(both)}"
            )
        return self

    @model_validator(mode="after")
    def _one_landing_branch_per_repo(self) -> ModelBranchProtectionAuditPolicy:
        repos = [entry.repo for entry in self.landing_branches]
        repeated = sorted({repo for repo in repos if repos.count(repo) > 1})
        if repeated:
            raise ValueError(
                f"a repository declares one landing branch only: {', '.join(repeated)}"
            )
        return self

    @model_validator(mode="after")
    def _review_verdict_requirements_are_well_formed(
        self,
    ) -> ModelBranchProtectionAuditPolicy:
        if self.review_verdict_required:
            if not self.review_verdict_context:
                raise ValueError(
                    "review_verdict_required is set but review_verdict_context is "
                    "empty: a requirement needs the context it must be present"
                )
            seen: set[tuple[str, str]] = set()
            for entry in self.review_verdict_required:
                pair = (entry.repo, entry.branch)
                if pair in seen:
                    raise ValueError(
                        f"a repository declares one review verdict requirement "
                        f"per branch: {entry.repo} {entry.branch}"
                    )
                seen.add(pair)
        return self


__all__ = ["OVERLAY_SCHEMA_VERSION", "ModelBranchProtectionAuditPolicy"]
