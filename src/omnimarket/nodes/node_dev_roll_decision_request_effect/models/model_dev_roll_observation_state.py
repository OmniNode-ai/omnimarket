# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The observation state node_dev_roll_decision_request_effect keeps."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.dev_roll.model_dev_tip_changed import (
    ModelDevTipChanged,
)


class ModelDevRollObservationState(BaseModel):
    """The pending tip events, the seen job ids and the last observed roll.

    The three ``last_roll`` fields are None together (no roll observed yet) or
    set together (the shadow roll one decided request rolled onto).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pending_events: tuple[ModelDevTipChanged, ...] = ()
    seen_job_ids: tuple[str, ...] = ()
    last_roll_job_id: str | None = None
    last_roll_git_ref: str | None = None
    last_roll_started_at: datetime | None = None

    @model_validator(mode="after")
    def _last_roll_fields_together(self) -> ModelDevRollObservationState:
        fields = (
            self.last_roll_job_id,
            self.last_roll_git_ref,
            self.last_roll_started_at,
        )
        if any(field is None for field in fields) and any(
            field is not None for field in fields
        ):
            raise ValueError("the last_roll fields are None together or set together")
        return self
