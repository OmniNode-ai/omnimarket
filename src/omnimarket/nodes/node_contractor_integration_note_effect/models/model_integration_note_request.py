# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input models for the contractor integration-note effect (OMN-17277).

The request carries only what the trigger observed about the merge plus the
contractor roster overlay. Everything else the note needs — the ticket's
assignee, the tags containing the merge commit, the notes already posted — is
resolved at the effect boundary by the handler's injected adapters, never
supplied by the caller. A caller-supplied assignee would let any merging lane
decide for itself whether a contractor is watching, which is exactly the
self-attestation this node exists to remove.

Related:
    - OMN-17277: integration note (WS2)
    - OMN-17274: Lakshman customer-plane validation charter (epic)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ModelContractorRosterEntry(BaseModel):
    """One external contractor whose assigned tickets earn an integration note.

    ``linear_user_id`` is the Linear user UUID, not a display name: names are
    editable by the user and collide, the UUID is the join key Linear itself
    uses on ``issue.assignee.id``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    linear_user_id: str = Field(
        ..., min_length=1, description="Linear user UUID of the contractor."
    )
    display_name: str = Field(
        ..., min_length=1, description="Human-readable name, used only in prose."
    )
    surfaces: tuple[str, ...] = Field(
        default=(),
        description=(
            "Charter surface rows this contractor probes (e.g. C1..C6). Used to "
            "say which surfaces a change may touch when the merging lane did "
            "not narrow it themselves."
        ),
    )


class ModelPinRecipe(BaseModel):
    """Template for the dev-only pin recipe a contractor runs to reach a change.

    Templated, not hardcoded: ``{repo}``, ``{repo_name}``, ``{repo_url}`` and
    ``{merge_sha}`` are substituted by the composer. A repo whose install verb
    differs from the default declares its own override in the roster overlay.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    template: str = Field(
        ..., min_length=1, description="Pin command template with placeholders."
    )


class ModelContractorRoster(BaseModel):
    """The configured contractor set plus the pin recipes for dev-only changes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contractors: tuple[ModelContractorRosterEntry, ...] = Field(
        default=(), description="Contractors whose tickets earn a note."
    )
    default_pin_recipe: ModelPinRecipe = Field(
        ..., description="Pin recipe used when a repo declares no override."
    )
    repo_pin_recipes: dict[str, ModelPinRecipe] = Field(
        default_factory=dict,
        description="Per-repo pin recipe overrides, keyed by owner/repo slug.",
    )


class ModelMergedPullRequest(BaseModel):
    """The merge facts observed by the trigger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., min_length=1, description="GitHub repo slug (owner/repo).")
    number: int = Field(..., gt=0, description="Merged PR number.")
    title: str = Field(..., description="PR title as merged.")
    body: str = Field(default="", description="PR body as merged (may be empty).")
    merge_sha: str = Field(..., min_length=7, description="Merge commit SHA.")
    merged_at: datetime = Field(..., description="When the PR merged (UTC).")
    base_ref: str = Field(..., min_length=1, description="Branch the PR merged into.")
    html_url: str = Field(..., min_length=1, description="Public URL of the PR.")


class ModelTicketFacts(BaseModel):
    """Linear ticket facts resolved at the effect boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    issue_id: str = Field(..., min_length=1, description="Linear issue UUID.")
    identifier: str = Field(..., min_length=1, description="Ticket key, e.g. OMN-123.")
    title: str = Field(default="", description="Ticket title.")
    assignee_linear_user_id: str | None = Field(
        default=None,
        description=(
            "Linear user UUID of the assignee. None means the ticket is "
            "unassigned — which is not the same as assigned to a non-contractor."
        ),
    )


class ModelIntegrationNoteRequest(BaseModel):
    """Definition-B request payload for the integration-note effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pull_request: ModelMergedPullRequest = Field(
        ..., description="The merge that may earn a note."
    )
    roster: ModelContractorRoster = Field(
        ..., description="Contractor roster resolved from the checked-in overlay."
    )
    checkout_path: Path = Field(
        ...,
        description=(
            "A checkout of pull_request.repo, with tags fetched, used to answer "
            "'is this merge in a released tag'. Required, and carried in the "
            "payload rather than bound into an adapter: it is a fact about THIS "
            "request, and a caller that cannot supply one cannot answer the "
            "note's reachability field at all — better to fail validation than "
            "to answer 'not released' from an empty tag list."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="Compose and report the note without writing it to Linear.",
    )


__all__ = [
    "ModelContractorRoster",
    "ModelContractorRosterEntry",
    "ModelIntegrationNoteRequest",
    "ModelMergedPullRequest",
    "ModelPinRecipe",
    "ModelTicketFacts",
]
