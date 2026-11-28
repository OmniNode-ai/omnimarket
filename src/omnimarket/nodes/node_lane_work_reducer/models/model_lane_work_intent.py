# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One intent the fold emits (OMN-26706)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneWorkIntent(BaseModel):
    """An action by the worker (kind agent) or by a node (kind is the action name).

    A terminal intent has kind terminal. `action_id` is <workflow id>-<seq>-<k>.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    actor: str
    name: str
    action_id: str
    inputs: dict[str, str] = Field(default_factory=dict)
    deadline_at: datetime | None = None


__all__ = ["ModelLaneWorkIntent"]
