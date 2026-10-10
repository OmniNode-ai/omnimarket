# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestrator's durable ``state_io`` row for one PR (OMN-19829).

``ModelPrLandingState`` is the reducer's row: the frozen fields of plan 5.1
revision 1, section 3, which the pure reducer reads and writes. This model
wraps it with what only the orchestrator keeps, so the reducer's seam stays
exactly as frozen:

* the per-PR read sequence (section 6): ``reads_issued`` is the last read id
  handed out, ``reads_answered`` the newest one applied. Reads are serialized
  by the one-in-flight rule (R4), so the sequence is monotonic, and a
  redelivered or reordered answer for an older read is dropped whole;
* the effect in flight (R4): at most one GitHub effect per PR, with the
  correlation id its completion must carry;
* the deduplication keys (F9): agent-needed per (head, reason), conflict requests
  per (head, base head) and terminals per (PR, episode);
* the facts the effects need that the reducer does not: the ETags of the two
  conditional reads, the PR's GraphQL node id, the check-run ids the last
  check read saw, the base branch and its head, whether GitHub reports armed,
  and when the head checks were last read.

``landing`` is None until the first snapshot has been applied: the first
autobind prompt carries no head, so the row exists only to order the read it
issues.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_agent_reason import (
    EnumPrLandingAgentReason,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_intent import (
    ModelPrLandingIntent,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_observation import (
    HEAD_SHA_PATTERN,
    REPOSITORY_PATTERN,
    fill_landing_key,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_state import (
    ModelPrLandingState,
)


class ModelPrLandingInFlight(BaseModel):
    """The one GitHub effect sent for this PR and not yet answered (R4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Deterministic id the effect's completion or failure carries."
    )
    intent: ModelPrLandingIntent = Field(..., description="The intent that was sent.")
    read_id: int | None = Field(
        default=None,
        ge=1,
        description="Set on a read_pr_state: the read sequence number it carries.",
    )
    sent_at: datetime = Field(..., description="When the dispatcher sent it.")


class ModelPrLandingAgentNeededKey(BaseModel):
    """One (head, reason) an agent-needed event was already emitted for (P4, F9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    head_sha: str | None = Field(default=None, pattern=HEAD_SHA_PATTERN)
    reason: EnumPrLandingAgentReason


class ModelPrLandingConflictKey(BaseModel):
    """One (head, base head) a conflict request was already emitted for (OMN-20750)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    head_sha: str = Field(..., pattern=HEAD_SHA_PATTERN)
    base_sha: str | None = Field(default=None, pattern=HEAD_SHA_PATTERN)


class ModelPrLandingCheckRunRef(BaseModel):
    """A check name and the workflow run that produced its newest copy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: str = Field(..., min_length=1)
    run_id: int = Field(..., ge=1)


class ModelPrLandingWorkflowRow(BaseModel):
    """The ``state_io`` payload of ``pr_landing_workflow_state``, keyed ``landing_key``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(..., pattern=REPOSITORY_PATTERN)
    pr_number: int = Field(..., ge=1)
    landing_key: str = Field(..., description="``owner/repo#123``, the row key.")
    landing: ModelPrLandingState | None = Field(
        default=None,
        description="The reducer's row; None until the first snapshot is applied.",
    )
    reads_issued: int = Field(
        default=0, ge=0, description="The last read id handed out (section 6)."
    )
    reads_answered: int = Field(
        default=0, ge=0, description="The newest read id whose answer was applied."
    )
    dispatches: int = Field(
        default=0,
        ge=0,
        description="Effects sent so far; seeds each effect's deterministic correlation id.",
    )
    pending_read: bool = Field(
        default=False,
        description=(
            "A read_pr_state is wanted but another effect is in flight. The "
            "read is sent before any queued outbox entry once the PR is free, "
            "so a snapshot is never older than the effects it orders."
        ),
    )
    effect_in_flight: ModelPrLandingInFlight | None = Field(default=None)
    agent_needed_sent: tuple[ModelPrLandingAgentNeededKey, ...] = Field(default=())
    conflict_requested: tuple[ModelPrLandingConflictKey, ...] = Field(
        default=(),
        description=(
            "The (head, base head) pairs a conflict request was sent for; "
            "a moved head or base is a new pair."
        ),
    )
    terminal_episodes: tuple[int, ...] = Field(
        default=(),
        description="Episodes a merged or closed terminal was already emitted for.",
    )
    pr_state_etag: str | None = Field(default=None, min_length=1)
    head_checks_etag: str | None = Field(default=None, min_length=1)
    head_checks_read_at: datetime | None = Field(default=None)
    pr_node_id: str | None = Field(default=None, min_length=1)
    base_ref: str | None = Field(default=None, min_length=1)
    merge_state_status: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "GitHub's mergeable_state from the newest PR read, for the arm "
            "gate's merge-state fact (OMN-20866)."
        ),
    )
    base_sha: str | None = Field(
        default=None,
        pattern=HEAD_SHA_PATTERN,
        description=(
            "The base branch's head from the newest PR read, half of the "
            "conflict request key (OMN-20750)."
        ),
    )
    github_armed: bool = Field(
        default=False,
        description=(
            "GitHub reported auto-merge armed or the PR in the merge queue on "
            "the newest PR read, whoever armed it (OMN-20750)."
        ),
    )
    check_runs: tuple[ModelPrLandingCheckRunRef, ...] = Field(default=())
    bound_expired_for: tuple[int, int] | None = Field(
        default=None,
        description=(
            "The (episode, state_entry_generation) whose completion bound was "
            "last applied, so each state entry expires at most once (R2b)."
        ),
    )
    updated_at: datetime = Field(..., description="Time of the input last applied.")
    # Well-known top-level columns omnibase_infra's state_io wiring denormalizes
    # (tenant_id, state, in_flight) are added by the codec at encode time.

    @model_validator(mode="before")
    @classmethod
    def _derive_landing_key(cls, data: object) -> object:
        return fill_landing_key(data)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.reads_answered > self.reads_issued:
            msg = "a read cannot be answered before it is issued"
            raise ValueError(msg)
        if self.landing is not None and self.landing.landing_key != self.landing_key:
            msg = "the reducer row belongs to this landing_key"
            raise ValueError(msg)
        if len(set(self.terminal_episodes)) != len(self.terminal_episodes):
            msg = "one terminal per episode"
            raise ValueError(msg)
        if len(set(self.agent_needed_sent)) != len(self.agent_needed_sent):
            msg = "one agent-needed per (head, reason)"
            raise ValueError(msg)
        if len(set(self.conflict_requested)) != len(self.conflict_requested):
            msg = "one conflict request per (head, base head)"
            raise ValueError(msg)
        return self


__all__: list[str] = [
    "ModelPrLandingAgentNeededKey",
    "ModelPrLandingCheckRunRef",
    "ModelPrLandingConflictKey",
    "ModelPrLandingInFlight",
    "ModelPrLandingWorkflowRow",
]
