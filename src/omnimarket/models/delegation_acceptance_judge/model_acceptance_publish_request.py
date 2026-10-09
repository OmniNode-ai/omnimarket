# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judged-acceptance events to hand to the local emit daemon."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation_acceptance_judge.model_delegation_acceptance_judged_event import (
    ModelDelegationAcceptanceJudgedEvent,
)


class ModelAcceptancePublishRequest(BaseModel):
    """The events to publish and, optionally, the emit daemon's socket path.

    An empty ``socket_path`` resolves the way every emit client does: ``ONEX_EMIT_SOCKET_PATH``,
    then the runtime directory, then the temp-directory fallback.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    events: tuple[ModelDelegationAcceptanceJudgedEvent, ...] = Field(min_length=1)
    socket_path: str = ""
