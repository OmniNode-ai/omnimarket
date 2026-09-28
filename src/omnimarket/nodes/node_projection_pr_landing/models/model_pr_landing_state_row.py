# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One upsert into ``pr_landing_state``, keyed (repository, pr_number)."""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.pr_landing.enum_pr_landing_agent_reason import (
    EnumPrLandingAgentReason,
)
from omnimarket.events.pr_landing.enum_pr_landing_state import (
    EnumPrLandingState,
)
from omnimarket.events.pr_landing.model_pr_landing_observation import (
    HEAD_SHA_PATTERN,
    REPOSITORY_PATTERN,
)
from omnimarket.nodes.node_projection_pr_landing.models.enum_pr_landing_projection_event_kind import (
    EnumPrLandingProjectionEventKind,
)


class ModelPrLandingStateRow(BaseModel):
    """What one landing event asserts about the PR's current row.

    ``seq`` is the orchestrator's per-key sequence and the only ordering
    authority: the writer's upsert refuses a write whose ``seq`` is below the
    stored one, in the conflict arm's WHERE. Several events describe one
    transition and share its ``seq`` (the transition itself, plus its
    agent-needed or terminal event); they merge into the row in any order.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(..., pattern=REPOSITORY_PATTERN)
    pr_number: int = Field(..., ge=1)
    seq: int = Field(..., ge=0)
    event_kind: EnumPrLandingProjectionEventKind = Field(...)
    state: EnumPrLandingState = Field(...)
    head_sha: str | None = Field(default=None, pattern=HEAD_SHA_PATTERN)
    trigger: str | None = Field(
        default=None,
        min_length=1,
        description="Set exactly on a transition: the state_machine trigger taken.",
    )
    episode: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Asserted only by a terminal, which carries the episode it ends "
            "(F10). A transition carries no episode; see opens_episode."
        ),
    )
    opens_episode: bool = Field(
        default=False,
        description=(
            "A transition out of CLOSED into any other state: the table's reopen "
            "rows, which increment the episode (F10, G5)."
        ),
    )
    agent_reason: EnumPrLandingAgentReason | None = Field(default=None)
    agent_detail: str | None = Field(default=None, min_length=1)
    terminal_at: datetime | None = Field(default=None)
    event_at: datetime = Field(...)

    @model_validator(mode="after")
    def _fields_match_the_event_kind(self) -> Self:
        kind = self.event_kind
        is_transition = kind is EnumPrLandingProjectionEventKind.TRANSITIONED
        if is_transition != (self.trigger is not None):
            msg = "trigger is set exactly on a transition"
            raise ValueError(msg)
        if self.opens_episode and not is_transition:
            msg = "only a transition opens an episode"
            raise ValueError(msg)
        is_terminal = kind in (
            EnumPrLandingProjectionEventKind.MERGED,
            EnumPrLandingProjectionEventKind.CLOSED,
        )
        if is_terminal != (self.episode is not None):
            msg = "episode is asserted exactly by a terminal"
            raise ValueError(msg)
        if is_terminal != (self.terminal_at is not None):
            msg = "terminal_at is set exactly by a terminal"
            raise ValueError(msg)
        is_agent = kind is EnumPrLandingProjectionEventKind.AGENT_NEEDED
        if is_agent != (self.agent_reason is not None):
            msg = "agent_reason is set exactly by an agent-needed event"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _times_carry_a_timezone(self) -> Self:
        for value in (self.event_at, self.terminal_at):
            if value is not None and value.utcoffset() is None:
                msg = "event times must carry a timezone"
                raise ValueError(msg)
        return self


__all__: list[str] = ["ModelPrLandingStateRow"]
