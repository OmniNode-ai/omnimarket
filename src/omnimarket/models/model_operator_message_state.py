# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public data boundary for the pure operator arrival/reply fold."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelOperatorMessageStateConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    arrival_kinds: tuple[str, ...]
    arrival_row_kinds: tuple[str, ...]
    reply_kinds: tuple[str, ...]
    reply_row_kinds: tuple[str, ...]
    message_id_cells: tuple[str, ...]
    source_id_cells: tuple[str, ...]
    answer_cells: tuple[str, ...]
    operator_recipient: str = Field(min_length=1)
    default_owner: str = Field(min_length=1)
    item_prefix: str = Field(min_length=1)
    id_chars: int = Field(ge=10)
    unanswered_state: Literal["unanswered"]
    answered_state: Literal["answered"]


class ModelOperatorMessageEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    owner_lane: str = Field(min_length=1)
    arrived_at: datetime
    state: Literal["unanswered", "answered"]
    age_minutes: float = Field(ge=0)
    answered_at: datetime | None = None
    evidence: tuple[str, ...] = ()


class ModelOperatorMessageStateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[str, ...]
    now: datetime
    config: ModelOperatorMessageStateConfig


class ModelOperatorMessageStateReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: datetime
    items: tuple[ModelOperatorMessageEntry, ...]
    unanswered: tuple[str, ...]
