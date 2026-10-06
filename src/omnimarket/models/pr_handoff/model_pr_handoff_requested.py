# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PR handoff command a lane publishes instead of deciding locally (OMN-20636)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.pr_handoff.enum_pr_handoff_mode import EnumPrHandoffMode
from omnimarket.models.pr_handoff.enum_pr_handoff_needs import EnumPrHandoffNeeds
from omnimarket.models.pr_handoff.model_pr_handoff_lab_proof import (
    ModelPrHandoffLabProof,
)

LANE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
REPO_PATTERN = r"^[A-Za-z0-9_.-]+$"
TICKET_PATTERN = r"^OMN-[0-9]+$"
SHORT_SHA_PATTERN = r"^[0-9a-f]{7,40}$"
DEFAULT_WAIT_BUDGET_S = 3600
MAX_WAIT_BUDGET_S = 86400


def handoff_key_for(repo: str, pr_number: int) -> str:
    """The workflow key: ``repo#n``, the form the ledger's ``pr=`` cell carries."""
    return f"{repo}#{pr_number}"


class ModelPrHandoffRequested(BaseModel):
    """One request to hand one PR, at one head, from one lane to the landing lane.

    ``correlation_id`` names the request and every event of its chain.
    ``handoff_key`` is derived from ``repo`` and ``pr_number`` and is the
    ``state_io`` key, so every message about the PR reaches the same row.

    The ledger-side gates of pr-handoff (inbox, routing RULING, CLAIM, friction,
    worktree closeout) stay with the requesting lane, which reads its ledger;
    the lane states their results here (``claim_tickets``, ``claim_prs``,
    ``friction``, ``worktree_cell``). Everything that depends on the live PR is
    decided by node_pr_handoff_decision_compute from the PR watcher's
    observations, never from a copy taken on the lane's host.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    handoff_key: str = Field(default="", description="Derived: repo#pr_number.")
    repo: str = Field(
        ..., pattern=REPO_PATTERN, description="Short name, as the PR watcher names it."
    )
    pr_number: int = Field(..., ge=1)
    expected_head_sha: str = Field(..., pattern=SHORT_SHA_PATTERN)
    lane: str = Field(..., pattern=LANE_PATTERN)
    to_lane: str = Field(default="landing-controller", pattern=LANE_PATTERN)
    needs: EnumPrHandoffNeeds = EnumPrHandoffNeeds.LAND
    mode: EnumPrHandoffMode = EnumPrHandoffMode.LANE
    msg_only: bool = False
    ticket: str | None = Field(
        default=None,
        pattern=TICKET_PATTERN,
        description="The --ticket flag: used only when the title carries no ticket.",
    )
    claim_tickets: tuple[Annotated[str, Field(pattern=TICKET_PATTERN)], ...] = ()
    claim_prs: tuple[str, ...] = Field(
        default=(), description="repo#n references the lane's CLAIMs name."
    )
    body_ticket_ids: tuple[Annotated[str, Field(pattern=TICKET_PATTERN)], ...] = Field(
        default=(),
        description="The OMN ids the PR body cites, read by the lane that wrote it.",
    )
    lab_proof: ModelPrHandoffLabProof | None = None
    friction: str = Field(default="none", min_length=1, max_length=500)
    delegation: str | None = Field(default=None, max_length=500)
    worktree_cell: str | None = Field(default=None, max_length=500)
    inbox_seen: str | None = Field(default=None, max_length=200)
    requesting_host: str = Field(..., min_length=1, max_length=100)
    requested_at: Annotated[datetime, AwareDatetime]
    wait_budget_s: int = Field(
        default=DEFAULT_WAIT_BUDGET_S, ge=60, le=MAX_WAIT_BUDGET_S
    )

    @model_validator(mode="before")
    @classmethod
    def _derive_handoff_key(cls, data: object) -> object:
        if isinstance(data, dict) and "repo" in data and "pr_number" in data:
            derived = handoff_key_for(str(data["repo"]), int(data["pr_number"]))
            supplied = data.get("handoff_key")
            if supplied and supplied != derived:
                msg = f"handoff_key {supplied!r} does not match {derived!r}"
                raise ValueError(msg)
            return {**data, "handoff_key": derived}
        return data

    @model_validator(mode="after")
    def _single_line_cells(self) -> ModelPrHandoffRequested:
        for name in ("friction", "delegation", "worktree_cell", "inbox_seen"):
            value = getattr(self, name)
            if value is not None and ("|" in value or "\n" in value):
                msg = f"{name} must be one line with no '|'"
                raise ValueError(msg)
        return self


__all__: list[str] = [
    "DEFAULT_WAIT_BUDGET_S",
    "LANE_PATTERN",
    "REPO_PATTERN",
    "SHORT_SHA_PATTERN",
    "TICKET_PATTERN",
    "ModelPrHandoffRequested",
    "handoff_key_for",
]
