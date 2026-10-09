# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What reached the emit daemon, or why nothing did."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation_acceptance_judge.enum_acceptance_publish_status import (
    EnumAcceptancePublishStatus,
)


class ModelAcceptancePublishResult(BaseModel):
    """The outcome of one publish: every event queued, or the socket that refused and how many got through.

    ``status`` uses the runtime's terminal vocabulary, so a failed publish ends the run with a
    non-zero exit instead of a success report. Events carry deterministic ids, so publishing the
    same events again after a failure is safe.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumAcceptancePublishStatus
    socket_path: str = Field(min_length=1)
    requested_count: int = Field(ge=0)
    published_count: int = Field(ge=0)
    reason: str = ""
