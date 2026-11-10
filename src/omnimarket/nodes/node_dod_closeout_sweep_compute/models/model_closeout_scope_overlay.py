# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed overlay of the closeout sweep's scope ranking (OMN-20655)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumRankRule(StrEnum):
    """One ordered ranking rule; the overlay lists the rules in the order they decide."""

    WAITING_ON = "waiting_on"
    HAS_CONTRACT = "has_contract"
    MERGED_IN_REVIEW = "merged_in_review"


class EnumCloneLocation(StrEnum):
    """Where a repository's clone sits relative to the registry root."""

    REGISTRY_ROOT = "registry_root"  # the registry root itself is the clone
    UNDER_REGISTRY = "under_registry"  # <registry root>/<name>
    BESIDE_REGISTRY = "beside_registry"  # <registry root>/../<name>


class ModelPrRepository(BaseModel):
    """One repository whose merges and open PRs the closer reads, with its own default branch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    default_branch: str = Field(min_length=1)
    location: EnumCloneLocation


class ModelCloseoutScopeOverlay(BaseModel):
    """Every value the scope ranking and the bundle read; none is a constant in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(ge=1)
    eligible_states: list[str] = Field(min_length=1)
    rank_rules: list[EnumRankRule]
    no_contract_reserve: int = Field(ge=0)
    # True: a ticket with no contract on a default branch (and no recorded post-merge condition)
    # is selected only through the reserve above, never into a free slot of the ranking.
    no_contract_only_by_reserve: bool
    bundle_max_bytes: int = Field(ge=1024)
    held_cap: int = Field(ge=0)
    waiting_cap: int = Field(ge=0)
    contract_repos: list[str]
    default_refs: list[str] = Field(min_length=1)
    no_contract_detail_prefix: str = Field(min_length=1)
    # The PR reader (OMN-20655): the repositories whose squash merges name a ticket's work, each
    # with the branch its merges land on, and what the watcher state must look like to be read.
    pr_repositories: list[ModelPrRepository] = Field(min_length=1)
    # A merged-commit ref for a repository not listed above (a check naming none, or another one).
    binding_default_branch: str = Field(min_length=1)
    evidence_repository: str = Field(min_length=1)
    evidence_subject_prefix: str = Field(min_length=1)
    watcher_state_schema: int = Field(ge=1)
    watcher_tick_max_age_seconds: int = Field(ge=1)
    watcher_resync_max_age_seconds: int = Field(ge=1)
    watcher_wait_seconds: int = Field(ge=0)

    def default_branches(self) -> dict[str, str]:
        """Repository name -> the branch its merges land on."""
        return {r.name: r.default_branch for r in self.pr_repositories}
