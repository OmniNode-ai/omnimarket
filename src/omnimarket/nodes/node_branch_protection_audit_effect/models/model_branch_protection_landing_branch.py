# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A repository's landing branch and the bypass actors its landing-only ruleset declares."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelBranchProtectionBypassActor(BaseModel):
    """One bypass actor as GitHub reports it on a ruleset."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # GitHub's actor_type, for example "Integration" or "OrganizationAdmin".
    actor_type: str
    # The App id or role id; GitHub reports an organization admin as 1.
    actor_id: int
    # "pull_request" for a landing-only ruleset, never "always".
    bypass_mode: str = "pull_request"

    def label(self) -> str:
        return f"{self.actor_type}:{self.actor_id}:{self.bypass_mode}"


class ModelBranchProtectionLandingBranch(BaseModel):
    """What the landing-only merge ruleset on one landing branch must look like."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    branch: str
    # The landing App and the organization admins, both in pull-request mode.
    bypass_actors: tuple[ModelBranchProtectionBypassActor, ...] = ()


__all__ = ["ModelBranchProtectionBypassActor", "ModelBranchProtectionLandingBranch"]
