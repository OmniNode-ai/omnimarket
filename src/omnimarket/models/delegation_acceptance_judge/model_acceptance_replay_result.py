# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The events a committed run replays as, or the refusals that stopped it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.delegation_acceptance_judge.enum_acceptance_publish_status import (
    EnumAcceptancePublishStatus,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_replay_issue import (
    ModelAcceptanceReplayIssue,
)
from omnimarket.models.delegation_acceptance_judge.model_delegation_acceptance_judged_event import (
    ModelDelegationAcceptanceJudgedEvent,
)


class ModelAcceptanceReplayResult(BaseModel):
    """All of the run's events, or none and the issues. Never part of a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumAcceptancePublishStatus
    events: tuple[ModelDelegationAcceptanceJudgedEvent, ...] = ()
    issues: tuple[ModelAcceptanceReplayIssue, ...] = ()
    skipped_unjudged_count: int = 0

    @model_validator(mode="after")
    def _events_xor_issues(self) -> ModelAcceptanceReplayResult:
        completed = self.status is EnumAcceptancePublishStatus.COMPLETED
        if completed and self.issues:
            raise ValueError("a completed replay carries no issues")
        if not completed and (self.events or not self.issues):
            raise ValueError("a failed replay carries issues and no events")
        return self
