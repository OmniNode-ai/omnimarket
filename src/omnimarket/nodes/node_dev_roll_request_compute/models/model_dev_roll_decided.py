# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The batching decision: roll now at a ref, or hold with a reason."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_dev_roll_request_compute.models.enum_dev_roll_action import (
    EnumDevRollAction,
)
from omnimarket.nodes.node_dev_roll_request_compute.models.enum_dev_roll_hold_reason import (
    EnumDevRollHoldReason,
)
from omnimarket.nodes.node_dev_roll_request_compute.models.model_dev_roll_event_refused import (
    ModelDevRollEventRefused,
)
from omnimarket.nodes.node_dev_roll_request_compute.models.model_dev_roll_request import (
    ModelDevRollRequest,
)


class ModelDevRollDecided(BaseModel):
    """In shadow mode ``would_request`` records what a live node would send.

    ``released_failed_roll_job_id`` names the failed previous roll whose hold this
    decision released after the declared bound; it is only set on ``roll_now``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    observed_at: datetime
    action: EnumDevRollAction
    hold_reason: EnumDevRollHoldReason | None
    hold_until: datetime | None
    shadow: bool
    would_request: ModelDevRollRequest | None
    rolled_event_ids: tuple[UUID, ...]
    newest_sha_by_repository: dict[str, str]
    oldest_pending_merged_at: datetime | None
    longest_wait_seconds: int = Field(ge=0)
    refused_events: tuple[ModelDevRollEventRefused, ...]
    released_failed_roll_job_id: str | None = None

    @model_validator(mode="after")
    def _action_matches_fields(self) -> ModelDevRollDecided:
        if self.action is EnumDevRollAction.ROLL_NOW:
            if self.would_request is None or self.hold_reason is not None:
                raise ValueError("roll_now needs a request and no hold reason")
        elif (
            self.hold_reason is None
            or self.would_request is not None
            or self.released_failed_roll_job_id is not None
        ):
            raise ValueError("hold needs a reason, no request and no released roll")
        return self
