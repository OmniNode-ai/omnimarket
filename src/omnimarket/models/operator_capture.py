# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed requests and results of node_operator_capture_compute (OMN-20905, OMN-20906, OMN-20907).

One model family for the four pure decisions the operator capture makes: what an operator
message says (classify), which ledger rows record it (rows), which earlier rulings it re-rules
(drift) and which captured asks are still open (open asks); plus the bus record the capture
consumes and the receipt it publishes. Every request carries its own clock; nothing here reads
one.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

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


#: The content kind the capture producer gives a prompt whose transcript origin is human: the
#: operator typed it (omniclaude hook_content_capture, recorded at Stop). Lane briefs, scheduled
#: prompts, task notifications and compaction summaries never carry it.
OPERATOR_PROMPT_KIND = "operator_prompt"


class ModelOperatorPromptRecord(BaseModel):
    """One record of the content-capture topic, as the operator capture reads it.

    ``extra="ignore"``: the record carries enrichment and redaction bookkeeping this node does not
    read, and a field the producer adds later must not fail it. ``said_at`` and ``prompt_id`` are
    plain strings because a fan-out whose redaction contract predates them hashes them; a value
    that does not parse as a time is ignored.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    session_id: str = Field(min_length=1)
    content_kind: str
    content: str = ""
    chunk_index: int = Field(default=0, ge=0)
    chunk_count: int = Field(default=1, ge=1)
    truncated: bool = False
    prompt_id: str | None = None
    said_at: str | None = None
    emitted_at: str | None = None
    actor: str | None = None

    @property
    def is_operator_prompt(self) -> bool:
        return self.content_kind == OPERATOR_PROMPT_KIND


class EnumOperatorCaptureStatus(StrEnum):
    """What the capture did with one operator prompt from the bus."""

    RECORDED = "recorded"
    PENDING = "pending"
    DUPLICATE = "duplicate"


class ModelOperatorCaptureReceipt(BaseModel):
    """The terminal event of one operator prompt: taken in, and how the inbox stood after."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    session_id: str
    prompt_id: str | None = None
    status: EnumOperatorCaptureStatus
    result: ModelCaptureProcessResult
    answered_at: datetime


class ModelOperatorCaptureTopics(BaseModel):
    """The topics the capture host reads and writes, from the effect node's contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompts: str
    receipt: str


__all__ = [
    "OPERATOR_PROMPT_KIND",
    "EnumClassifierSource",
    "EnumDriftRelation",
    "EnumOperatorCaptureStatus",
    "EnumPromptOrigin",
    "EnumUtteranceKind",
    "ModelCaptureDigestRequest",
    "ModelCaptureDigestResult",
    "ModelCaptureProcessRequest",
    "ModelCaptureProcessResult",
    "ModelCaptureRows",
    "ModelCaptureRowsRequest",
    "ModelDriftMatch",
    "ModelOpenAsk",
    "ModelOpenAsks",
    "ModelOpenAsksRequest",
    "ModelOperatorCaptureReceipt",
    "ModelOperatorCaptureTopics",
    "ModelOperatorPromptRecord",
    "ModelPriorRuling",
    "ModelRulingDrift",
    "ModelRulingDriftRequest",
    "ModelUtterance",
    "ModelUtteranceClassification",
    "ModelUtteranceClassifyRequest",
    "ModelUtteranceItem",
]
