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


__all__ = ["OVERLAY_SCHEMA_VERSION", "ModelBranchProtectionAuditPolicy"]
