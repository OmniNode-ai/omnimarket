# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared typed boundary for promise classification, measurement and capture."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.operator_capture import (
    EnumClassifierSource,
    ModelOperatorPromptRecord,
    ModelSentenceDisposition,
    ModelUtterance,
)


class ModelPromisePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["shadow", "active"]
    orchestrator_lane_pattern: str = Field(min_length=1)
    default_owner: str = Field(min_length=1)
    session_lanes: dict[str, str]
    sentence_pattern: str = Field(min_length=1)
    commitment_separator_pattern: str = Field(min_length=1)
    promise_patterns: tuple[str, ...] = Field(min_length=1)
    uncertain_patterns: tuple[str, ...] = Field(min_length=1)
    machine_prefixes: tuple[str, ...] = Field(min_length=1)
    machine_patterns: tuple[str, ...] = Field(min_length=1)
    heuristic_confidence: float = Field(ge=0, le=1)
    uncertain_confidence: float = Field(ge=0, le=1)
    confidence_floor: float = Field(ge=0, le=1)
    minimum_replies: int = Field(ge=60)
    minimum_precision: float | None = Field(ge=0, le=1)
    minimum_recall: float | None = Field(ge=0, le=1)
    interval_confidence: float = Field(gt=0, lt=1)
    bar_statistic: Literal["estimate", "lower_bound"]
    classifier_revision: str = Field(min_length=1)
    id_chars: int = Field(ge=16, le=64)
    subject_terms: int = Field(ge=1)
    row_lane: str = Field(min_length=1)
    row_actor: str = Field(min_length=1)
    disposition_kind: str = Field(min_length=1)
    chat_item: str = Field(min_length=1)


class ModelPromiseMetric(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    successes: int = Field(ge=0)
    total: int = Field(ge=0)
    estimate: float | None = Field(ge=0, le=1)
    lower: float = Field(ge=0, le=1)
    upper: float = Field(ge=0, le=1)


class ModelPromiseBarReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    corpus_id: str
    origin: Literal["corpus", "synthetic"]
    labelled_by: str
    replies: int = Field(ge=0)
    true_positive: int = Field(ge=0)
    false_positive: int = Field(ge=0)
    false_negative: int = Field(ge=0)
    precision: ModelPromiseMetric
    recall: ModelPromiseMetric
    interval_confidence: float
    policy_digest: str
    passed: bool
    reasons: tuple[str, ...]


class ModelPromiseItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1)
    kind: Literal["promise"] = "promise"
    sentence: int = Field(ge=1)
    quote: str = Field(min_length=1)
    subject: str
    confidence: float = Field(ge=0, le=1)
    state: Literal["candidate", "open"]
    owner: str = Field(min_length=1)


class ModelPromiseClassifyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    utterance: ModelUtterance
    policy: ModelPromisePolicy
    delegated_response: str | None = None
    delegated_model: str | None = None
    delegated_error: str | None = None
    evaluation: ModelPromiseBarReport | None = None


class ModelPromiseClassification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_id: str
    classifier: EnumClassifierSource
    classifier_model: str | None = None
    policy_digest: str
    reason: str
    machine: bool = False
    items: tuple[ModelPromiseItem, ...] = ()
    dispositions: tuple[ModelSentenceDisposition, ...] = ()
    low_confidence: int = Field(default=0, ge=0)


class ModelPromiseDelegateItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    sentence: int = Field(ge=1)
    kind: Literal["promise", "chat"]
    quote: str = Field(min_length=1)
    subject: str
    confidence: float = Field(ge=0, le=1)


class ModelPromiseDelegateAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    items: tuple[ModelPromiseDelegateItem, ...]


class ModelPromiseDelegateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    response: str | None = None
    model: str | None = None
    error: str | None = None


class ModelPromiseLabel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ModelPromiseClassifyRequest
    promise_sentences: tuple[int, ...]


class ModelPromiseBarRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    corpus_id: str = Field(min_length=1)
    origin: Literal["corpus", "synthetic"]
    labelled_by: str = Field(min_length=1)
    samples: tuple[ModelPromiseLabel, ...]
    policy: ModelPromisePolicy


class ModelPromiseCaptureRecord(ModelOperatorPromptRecord):
    """The existing Stop content-capture wire, with source/chunk identity retained."""

    correlation_id: str | None = None
    content_sha256: str | None = None
    capture_id: str | None = None
    content_record_id: str | None = None
    agent_id: str | None = None


class ModelPromiseStoredCapture(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    utterance: ModelUtterance
    classification: ModelPromiseClassification
    rows: tuple[str, ...]
    ledger_rows: tuple[str, ...]


class ModelPromiseCaptureReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_id: str | None = None
    status: Literal["recorded", "duplicate", "pending", "ignored"]
    rows: tuple[str, ...] = ()
    ledger_rows: tuple[str, ...] = ()
    candidate_count: int = Field(ge=0)
    low_confidence_count: int = Field(ge=0)


class ModelPromiseRenderRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    captures: tuple[ModelPromiseStoredCapture, ...]
    now: datetime


class ModelPromiseRenderResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    candidate_count: int = Field(ge=0)
    low_confidence_count: int = Field(ge=0)


class ModelPromiseEffectOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    store_path: str = Field(min_length=1)
    assistant_reply_kind: str = Field(min_length=1)
    policy: ModelPromisePolicy
