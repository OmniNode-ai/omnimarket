# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed snapshot and results; capture classifications are deliberately not inputs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelTranscriptRecord(BaseModel):
    """Transcript transport record; unrelated transcript metadata is preserved."""

    model_config = ConfigDict(frozen=True, extra="allow")
    type: str
    uuid: str | None = None
    timestamp: AwareDatetime | None = None
    message: dict[str, Any] = Field(default_factory=dict)
    is_sidechain: bool = Field(default=False, alias="isSidechain")
    is_meta: bool = Field(default=False, alias="isMeta")


class ModelMessageArrival(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    message_id: str
    session_id: str
    arrived_at: AwareDatetime
    native_id: str = ""
    content_sha256: str = ""


class ModelUnansweredRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    session_id: str
    now: AwareDatetime
    records: tuple[ModelTranscriptRecord, ...] = ()
    arrivals: tuple[ModelMessageArrival, ...] = ()
    item_rows: tuple[str, ...] = ()


class ModelUnansweredMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    message_id: str
    session_id: str
    arrived_at: AwareDatetime
    age_seconds: float
    words: str | None = None


class ModelUnansweredResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    unanswered: tuple[ModelUnansweredMessage, ...]
    answered_ids: tuple[str, ...]


class ModelAnswerWordsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    message_id: str
    acknowledgement_rows: tuple[str, ...] = ()
    content_store_words: str | None = None
    resend_text: str = Field(min_length=1)


class ModelAnswerWordsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    message_id: str
    words: str | None
    source: Literal["acknowledgement", "content_store", "unreadable"]
    reply: str | None = None
    leave_open: bool = False
    flagged: bool = False


class ModelStructuralLabel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    message_id: str
    assistant_text_addresses_message: bool


class ModelStructuralSample(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    sample_id: str = Field(min_length=1)
    labels: tuple[ModelStructuralLabel, ...]


class ModelStructuralMissRate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    sample_id: str
    sample_size: int
    miss_count: int
    miss_rate: float
    interval_low: float
    interval_high: float
    confidence_level: float
