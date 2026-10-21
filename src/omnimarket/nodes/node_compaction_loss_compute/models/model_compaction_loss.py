# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the compaction loss compute node (OMN-21122)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from omnimarket.nodes.node_compaction_loss_compute.models.model_compaction_loss_weights import (
    ModelCompactionLossWeights,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumMustCarryKind(StrEnum):
    LANE_CLAIM = "lane_claim"
    PROMISE = "promise"
    RULING = "ruling"
    PR = "pr"
    TICKET = "ticket"
    OPERATOR_INSTRUCTION = "operator_instruction"
    OPERATOR_MODE = "operator_mode"


class ModelCompactionPairsRequest(_Frozen):
    """A session's transcript records, raw and in file order."""

    session_id: str
    transcript_path: str | None = None
    records: tuple[dict[str, JsonValue], ...]
    weights: ModelCompactionLossWeights = Field(
        default_factory=ModelCompactionLossWeights
    )


class ModelCompactionLossRequest(_Frozen):
    """One compaction: the window it replaced and the summary that replaced it."""

    session_id: str
    compaction_id: str
    trigger: str | None = None
    transcript_path: str | None = None
    pre_tokens: int | None = None
    post_tokens: int | None = None
    pre_text: str = ""
    pre_user_text: str = ""
    pre_assistant_text: str = ""
    post_summary: str
    weights: ModelCompactionLossWeights = Field(
        default_factory=ModelCompactionLossWeights
    )


class ModelCompactionPairs(_Frozen):
    session_id: str
    pairs: tuple[ModelCompactionLossRequest, ...]


class ModelMustCarryItem(_Frozen):
    kind: EnumMustCarryKind
    key: str
    text: str
    required_spans: tuple[str, ...] = Field(min_length=1)
    survived: bool


class ModelKindRecall(_Frozen):
    kind: EnumMustCarryKind
    total: int
    survived: int
    recall: float


class ModelLossExpected(_Frozen):
    """Same field names as the replay eval presence case's expected quote."""

    statement_id: str
    quote: str | None = None
    required_spans: tuple[str, ...]


class ModelLossEvalCase(_Frozen):
    """Same field names as the replay eval presence case: swap package_text for a pack."""

    case_id: str
    candidate_id: str
    package_text: str
    expected: tuple[ModelLossExpected, ...]


class ModelCompactionLossResult(_Frozen):
    session_id: str
    compaction_id: str
    trigger: str | None
    transcript_path: str | None
    pre_tokens: int | None
    post_tokens: int | None
    items: tuple[ModelMustCarryItem, ...]
    items_total: int
    items_survived: int
    overall_recall: float | None
    recall_by_kind: tuple[ModelKindRecall, ...]
    dropped: tuple[ModelMustCarryItem, ...]
    eval_case: ModelLossEvalCase
    weights: ModelCompactionLossWeights = Field(
        default_factory=ModelCompactionLossWeights
    )
    # None also permits reading historical results that predate weighting.
    weighted_recall: float | None = Field(default=None, ge=0, le=1)


__all__ = [
    "EnumMustCarryKind",
    "ModelCompactionLossRequest",
    "ModelCompactionLossResult",
    "ModelCompactionLossWeights",
    "ModelCompactionPairs",
    "ModelCompactionPairsRequest",
    "ModelKindRecall",
    "ModelLossEvalCase",
    "ModelLossExpected",
    "ModelMustCarryItem",
]
