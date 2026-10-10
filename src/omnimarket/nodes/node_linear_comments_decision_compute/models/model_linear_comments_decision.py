# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the hourly Linear-comments sweep decisions (OMN-20680)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from omnibase_core.types import JsonType
from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumLinearCommentsDecisionKind(StrEnum):
    """Which decision the caller asks for; one request carries one decision."""

    DECIDE_ITEMS = "decide_items"
    BUILD_PROMPT = "build_prompt"
    CHECK_DRAFT = "check_draft"
    DETECT_TRUNCATION = "detect_truncation"
    CLASSIFY_HELD = "classify_held"
    TALLY = "tally"
    METRICS_ROW = "metrics_row"


class EnumAuthorKind(StrEnum):
    HUMAN = "human"
    AUTOMATION = "automation"
    OPERATOR_SELF = "operator_self"
    UNKNOWN = "unknown"


class EnumHeldClass(StrEnum):
    """Closed set of reasons a human ask reached drafting and came back empty."""

    FLOOR_REFUSED = "floor_refused"
    TRUNCATED = "truncated"
    UNSOURCED = "unsourced"
    TRANSPORT = "transport"
    TIMEOUT = "timeout"


class ModelCollectedItem(BaseModel):
    """One Linear comment as the collector returned it."""

    model_config = ConfigDict(frozen=True, extra="allow")

    ticket: str
    ticket_title: str
    comment_id: str
    comment_url: str = ""
    author_role: str
    author_kind: str = ""
    posted_at: str
    text: str
    needs_reply: bool
    asks: tuple[str, ...] = ()
    already_answered_by: str = ""


class ModelReplyDraft(BaseModel):
    """A drafted reply. comment_id may be absent: the schema requires it, so absence is a mismatch."""

    model_config = ConfigDict(frozen=True, extra="allow")

    ticket: str = ""
    comment_id: str | None = None
    their_summary: str = ""
    proposed_reply: str = ""
    facts_cited: tuple[str, ...] = ()
    open_points_for_operator: tuple[str, ...] = ()
    confidence: str = ""


class ModelDelegateOutcome(BaseModel):
    """The parts of a delegated call's result that decide whether it was cut off."""

    model_config = ConfigDict(frozen=True)

    finish_reason: str | None = None
    truncated: bool | None = None
    response_head: str | None = None
    response_tail: str | None = None


class ModelHeldInput(BaseModel):
    """Where an item was lost and why; reason_class, when given, is trusted."""

    model_config = ConfigDict(frozen=True)

    stage: str = ""
    reason: str = ""
    truncated: bool = False
    reason_class: str | None = None


class ModelLinearCommentsDecisionRequest(BaseModel):
    """One decision request; the fields a kind needs are validated, the rest must be absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumLinearCommentsDecisionKind
    items: tuple[ModelCollectedItem, ...] | None = None
    item: ModelCollectedItem | None = None
    facts: str | None = None
    draft: ModelReplyDraft | None = None
    outcome: ModelDelegateOutcome | None = None
    held: tuple[ModelHeldInput, ...] | None = None
    needing_count: int | None = Field(default=None, ge=0)
    accepted_count: int | None = Field(default=None, ge=0)
    receipt: dict[str, JsonType] | None = None

    @model_validator(mode="after")
    def _kind_needs_its_fields(self) -> Self:
        needs: dict[EnumLinearCommentsDecisionKind, tuple[str, ...]] = {
            EnumLinearCommentsDecisionKind.DECIDE_ITEMS: ("items",),
            EnumLinearCommentsDecisionKind.BUILD_PROMPT: ("item", "facts"),
            EnumLinearCommentsDecisionKind.CHECK_DRAFT: ("draft", "item", "facts"),
            EnumLinearCommentsDecisionKind.DETECT_TRUNCATION: ("outcome",),
            EnumLinearCommentsDecisionKind.CLASSIFY_HELD: ("held",),
            EnumLinearCommentsDecisionKind.TALLY: (
                "held",
                "needing_count",
                "accepted_count",
            ),
            EnumLinearCommentsDecisionKind.METRICS_ROW: ("receipt",),
        }
        wanted = needs[self.kind]
        supplied = {
            name
            for name in (
                "items",
                "item",
                "facts",
                "draft",
                "outcome",
                "held",
                "needing_count",
                "accepted_count",
                "receipt",
            )
            if getattr(self, name) is not None
        }
        missing = [name for name in wanted if name not in supplied]
        if missing:
            raise ValueError(f"{self.kind.value} requires {', '.join(missing)}")
        extra = sorted(supplied - set(wanted))
        if extra:
            raise ValueError(f"{self.kind.value} does not take {', '.join(extra)}")
        return self


class ModelItemDecision(BaseModel):
    """THE decision for one comment: draft or not, with the reason the decision produced."""

    model_config = ConfigDict(frozen=True)

    ticket: str
    comment_id: str
    comment_url: str
    author_role: str
    author_kind: EnumAuthorKind
    draft: bool
    reason_class: str
    reason: str


class ModelDraftCitationCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    reason: str
    foreign_ids: tuple[str, ...]
    comment_id_mismatch: bool


class ModelTruncationCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    truncated: bool
    signals: tuple[str, ...]


class ModelAttemptRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    tier: JsonType = None
    backend_id: JsonType = None
    model_id: JsonType = None
    quality_score: JsonType = None
    decision: JsonType = None
    cost_usd: JsonType = None


class ModelMetricsRow(BaseModel):
    """Which model answered, tokens, wall time and whether the draft was accepted."""

    model_config = ConfigDict(frozen=True)

    model: JsonType = None
    backend_id: JsonType = None
    tier: JsonType = None
    total_tokens: JsonType = None
    input_tokens: JsonType = None
    output_tokens: JsonType = None
    latency_ms: JsonType = None
    wall_ms: JsonType = None
    cost_usd: JsonType = None
    cost_savings_usd: JsonType = None
    premium_counterfactual_model: JsonType = None
    attempts: JsonType = None
    escalations: JsonType = None
    accepted: bool
    tried: tuple[ModelAttemptRow, ...]


class ModelLinearCommentsDecisionResult(BaseModel):
    """The answer to one request; exactly the fields of the requested kind are set."""

    model_config = ConfigDict(frozen=True)

    kind: EnumLinearCommentsDecisionKind
    decisions: tuple[ModelItemDecision, ...] | None = None
    selected_comment_ids: tuple[str, ...] | None = None
    prompt: str | None = None
    citation: ModelDraftCitationCheck | None = None
    truncation: ModelTruncationCheck | None = None
    held_classes: tuple[EnumHeldClass, ...] | None = None
    held_by_class: dict[str, int] | None = None
    degraded: bool | None = None
    metrics: ModelMetricsRow | None = None
    status: Literal["decided"] = "decided"
