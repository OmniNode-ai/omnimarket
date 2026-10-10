# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed requests and results of node_operator_capture_compute (OMN-20905, OMN-20906, OMN-20907).

One model family for the four pure decisions the operator capture makes: what an operator
message says (classify), which ledger rows record it (rows), which earlier rulings it re-rules
(drift), which captured asks are still open (open asks), and whether a dispatch may proceed
(guard). Every request carries its own clock; nothing here reads one.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from omnibase_core.types import JsonType
from pydantic import BaseModel, ConfigDict, Field


class EnumUtteranceKind(StrEnum):
    """What one item of an operator message is."""

    DECISION = "decision"
    ASK = "ask"
    IDEA = "idea"
    PREFERENCE = "preference"
    STATUS_QUESTION = "status_question"
    CHAT = "chat"


class EnumClassifierSource(StrEnum):
    """Who classified the message: the delegated model, or the deterministic fallback."""

    DELEGATED = "delegated"
    HEURISTIC = "heuristic"


class EnumPromptOrigin(StrEnum):
    """Whether a prompt is the operator's own words or text a machine injected."""

    OPERATOR = "operator"
    MACHINE = "machine"


class EnumDriftRelation(StrEnum):
    """How a captured decision relates to an earlier one on the same subject."""

    REAFFIRMS = "reaffirms"
    CONTRADICTS = "contradicts"


class EnumGuardVerdict(StrEnum):
    ALLOW = "allow"
    REFUSE = "refuse"


class ModelUtterance(BaseModel):
    """One operator message as captured, with where it came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_id: str = Field(min_length=1)
    text: str
    session_id: str = Field(min_length=1)
    source: str = Field(
        min_length=1, description="claude-code:local, claude-code:remote, slack"
    )
    captured_at: datetime


class ModelUtteranceClassifyRequest(BaseModel):
    """The message and, when the delegated model answered, its raw answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    utterance: ModelUtterance
    delegated_response: str | None = None
    delegated_model: str | None = None
    delegated_error: str | None = None


class ModelUtteranceItem(BaseModel):
    """One item of a message: its kind, the exact words, and a short subject."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str
    kind: EnumUtteranceKind
    quote: str = Field(min_length=1, description="an exact substring of the message")
    subject: str
    confidence: float = Field(ge=0.0, le=1.0)


class ModelUtteranceClassification(BaseModel):
    """Every item of one message, who classified it and why that classifier answered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_id: str
    origin: EnumPromptOrigin
    classifier: EnumClassifierSource
    classifier_model: str | None = None
    reason: str
    items: tuple[ModelUtteranceItem, ...] = ()
    drops: tuple[str, ...] = Field(
        default=(), description="ask ids the operator dropped"
    )


class ModelPriorRuling(BaseModel):
    """An earlier RULING row or captured decision, as the drift check reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stamp: str
    lane: str
    text: str = Field(
        description="the question and the verbatim words, or the captured quote"
    )


class ModelRulingDriftRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    item: ModelUtteranceItem
    prior: tuple[ModelPriorRuling, ...] = ()
    said_at: datetime | None = Field(
        default=None,
        description="when the operator said it; rulings stamped at or after it are not earlier",
    )


class ModelDriftMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stamp: str
    lane: str
    relation: EnumDriftRelation
    shared_terms: tuple[str, ...]


class ModelRulingDrift(BaseModel):
    """The earlier rulings a decision re-rules; empty when the subject is new."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    matches: tuple[ModelDriftMatch, ...] = ()

    @property
    def contradicts(self) -> bool:
        return any(m.relation is EnumDriftRelation.CONTRADICTS for m in self.matches)


class ModelCaptureRowsRequest(BaseModel):
    """A classification, the drift of each decision item, and the stamp to write rows at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    utterance: ModelUtterance
    classification: ModelUtteranceClassification
    drift: dict[str, ModelRulingDrift] = Field(
        default_factory=dict, description="by item_id"
    )
    stamp: datetime
    lane: str = "operator-capture"
    max_quote_chars: int = Field(default=1500, ge=80)


class ModelCaptureRows(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[str, ...] = ()


class ModelOpenAsksRequest(BaseModel):
    """Ledger rows (live file and recent rolls) and the evaluating time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[str, ...]
    now: datetime
    overdue_hours: float = Field(default=24.0, gt=0)
    digest_limit: int = Field(default=15, ge=1)


class ModelOpenAsk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ask_id: str
    stamp: str
    session_id: str
    words: str
    age_hours: float
    overdue: bool


class ModelOpenAsks(BaseModel):
    """Open asks oldest first, how many closed and dropped, and the digest text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    open_asks: tuple[ModelOpenAsk, ...] = ()
    closed: int = 0
    dropped: int = 0
    digest: str = ""

    @property
    def overdue(self) -> tuple[ModelOpenAsk, ...]:
        return tuple(a for a in self.open_asks if a.overdue)


class ModelDispatchGuardRequest(BaseModel):
    """A PreToolUse payload's facts and the capture ids recorded for its session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    agent_id: str | None = None
    headless: bool = False
    latest_operator_text: str | None = Field(
        default=None,
        description="the newest operator message in the transcript, if any",
    )
    latest_operator_digest: str | None = None
    captured_digests: frozenset[str] = frozenset()


class ModelDispatchGuardVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumGuardVerdict
    reason: str = ""
    uncaptured_text: str | None = Field(
        default=None, description="the operator message to capture now, on a refusal"
    )


class ModelCaptureProcessRequest(BaseModel):
    """One run of the capture worker over the local inbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    store_dir: Path
    ledger_path: Path
    max_captures: int = Field(default=20, ge=1)
    delegate: bool = True


class ModelCaptureProcessResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    processed: int = 0
    rows_appended: int = 0
    machine: int = 0
    pending: int = 0
    errors: tuple[str, ...] = ()


class ModelCaptureDigestRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    store_dir: Path
    ledger_path: Path
    now: datetime
    push_overdue: bool = False


class ModelCaptureDigestResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    digest: str
    open_asks: int
    overdue: int
    pending_captures: int
    pushed: tuple[str, ...] = ()
    push_error: str | None = None


class ModelCaptureGuardRequest(BaseModel):
    """A PreToolUse payload and the store it is judged against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    store_dir: Path
    payload: dict[str, JsonType]
    env: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "EnumClassifierSource",
    "EnumDriftRelation",
    "EnumGuardVerdict",
    "EnumPromptOrigin",
    "EnumUtteranceKind",
    "ModelCaptureDigestRequest",
    "ModelCaptureDigestResult",
    "ModelCaptureGuardRequest",
    "ModelCaptureProcessRequest",
    "ModelCaptureProcessResult",
    "ModelCaptureRows",
    "ModelCaptureRowsRequest",
    "ModelDispatchGuardRequest",
    "ModelDispatchGuardVerdict",
    "ModelDriftMatch",
    "ModelOpenAsk",
    "ModelOpenAsks",
    "ModelOpenAsksRequest",
    "ModelPriorRuling",
    "ModelRulingDrift",
    "ModelRulingDriftRequest",
    "ModelUtterance",
    "ModelUtteranceClassification",
    "ModelUtteranceClassifyRequest",
    "ModelUtteranceItem",
]
