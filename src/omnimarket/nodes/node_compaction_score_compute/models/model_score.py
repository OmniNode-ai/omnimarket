# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs and results for scoring a compacted context from recordings.

The ground-truth item shape is defined here, not imported from the ground-truth
node: one node never imports another node's models. The answers are recorded model
outputs; the scorer never calls a model. The request carries an ``arm`` label for
the caller's bookkeeping only. The fold never reads it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Nonempty = Annotated[str, Field(min_length=1)]

__all__ = [
    "EnumControl",
    "EnumItemKind",
    "EnumStratum",
    "EnumVerdict",
    "ModelControlCheck",
    "ModelGroundTruthItem",
    "ModelLedgerPrefix",
    "ModelMentionScore",
    "ModelPrecisionScore",
    "ModelRecallScore",
    "ModelRecordedAnswer",
    "ModelScoreRequest",
    "ModelScoreResult",
    "ModelStaleMemory",
    "ModelVerdictCounts",
]


class EnumItemKind(StrEnum):
    RULING = "RULING"
    OPERATOR_CONSENT = "OPERATOR_CONSENT"
    OPEN_CLAIM = "OPEN_CLAIM"
    OPEN_ASK = "OPEN_ASK"
    PROMISE_SENT = "PROMISE_SENT"
    DRAFT_UNSENT = "DRAFT_UNSENT"
    HOLD = "HOLD"
    UNACKED_MSG = "UNACKED_MSG"


DECISION_KINDS = frozenset({EnumItemKind.RULING, EnumItemKind.OPERATOR_CONSENT})
OPEN_LOOP_KINDS = frozenset(
    {
        EnumItemKind.OPEN_CLAIM,
        EnumItemKind.OPEN_ASK,
        EnumItemKind.PROMISE_SENT,
        EnumItemKind.DRAFT_UNSENT,
        EnumItemKind.UNACKED_MSG,
    }
)


class EnumStratum(StrEnum):
    LEDGER = "ledger"
    PLANTED = "planted"


class EnumVerdict(StrEnum):
    RECALLED = "RECALLED"
    ABSENT = "ABSENT"
    CONTRADICTED = "CONTRADICTED"
    UNAVAILABLE = "UNAVAILABLE"


class EnumControl(StrEnum):
    ORACLE = "oracle"
    EMPTY = "empty"
    INVERTED = "inverted"


class ModelGroundTruthItem(BaseModel):
    """One must-survive item: its stamp, kind, probe question and the operator's quote."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    key: Nonempty
    kind: EnumItemKind
    stratum: EnumStratum = EnumStratum.LEDGER
    question: str = ""
    quote: str = ""
    names: tuple[Nonempty, ...] = ()

    @model_validator(mode="after")
    def check_shape(self) -> ModelGroundTruthItem:
        if self.kind in DECISION_KINDS and not self.quote.strip():
            raise ValueError("A decision item needs the operator's quote")
        if self.stratum is EnumStratum.PLANTED and self.kind not in DECISION_KINDS:
            raise ValueError("Planted items are rulings, scored for recall only")
        return self

    @property
    def mention_names(self) -> tuple[str, ...]:
        return self.names or (self.key,)


class ModelRecordedAnswer(BaseModel):
    """One recorded answer attempt. Attempts are append-only; the latest valid one counts."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    item_key: Nonempty
    repeat: Annotated[int, Field(strict=True, ge=0)]
    attempt: Annotated[int, Field(strict=True, ge=0)] = 0
    manifest_digest: Nonempty
    available: bool = True
    text: str = ""
    judged: EnumVerdict | None = None

    @model_validator(mode="after")
    def check_judged(self) -> ModelRecordedAnswer:
        if self.judged is EnumVerdict.UNAVAILABLE:
            raise ValueError("A judge verdict is RECALLED, ABSENT or CONTRADICTED")
        return self


class ModelLedgerPrefix(BaseModel):
    """What the pinned ledger prefix holds at the boundary instant."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tickets: tuple[str, ...] = ()
    pull_requests: tuple[str, ...] = ()
    lanes: tuple[str, ...] = ()
    shas: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    timestamps: tuple[str, ...] = ()
    closed_lanes: tuple[str, ...] = ()
    superseded_rulings: tuple[str, ...] = ()
    amended_rulings: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class ModelScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: Nonempty
    arm: str | None = None
    boundary_at: AwareDatetime
    manifest_digest: Nonempty
    repeats: Annotated[int, Field(strict=True, ge=1)] = 3
    items: tuple[ModelGroundTruthItem, ...]
    answers: tuple[ModelRecordedAnswer, ...] = ()
    context_text: str = ""
    open_probe_answers: tuple[ModelRecordedAnswer, ...] = ()
    ledger: ModelLedgerPrefix = Field(default_factory=ModelLedgerPrefix)

    @model_validator(mode="after")
    def check_request(self) -> ModelScoreRequest:
        keys = [item.key for item in self.items]
        if len(set(keys)) != len(keys):
            raise ValueError("Item keys must be unique")
        known = set(keys)
        for answer in (*self.answers, *self.open_probe_answers):
            if answer.manifest_digest != self.manifest_digest:
                raise ValueError("An answer under another manifest digest is refused")
            if answer.repeat >= self.repeats:
                raise ValueError("An answer repeat is outside the declared repeats")
        for answer in self.answers:
            if answer.item_key not in known:
                raise ValueError("An answer names an item that is not in the set")
        return self


class ModelVerdictCounts(BaseModel):
    """ABSENT and CONTRADICTED are separate fields: a session that knows it forgot asks."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    recalled: int = 0
    absent: int = 0
    contradicted: int = 0
    unavailable: int = 0


class ModelRecallScore(BaseModel):
    """S1 over one stratum. Trials are item-by-repeat pairs; UNAVAILABLE counts in the total."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    stratum: EnumStratum
    items: int
    trials: int
    judged: ModelVerdictCounts
    lexical_recalled: int
    recall: float | None
    recall_wilson_lower: float | None
    contradiction: float | None
    lexical_recall: float | None
    item_verdicts: dict[str, tuple[EnumVerdict, ...]]


class ModelMentionScore(BaseModel):
    items: int
    mentioned: int
    mention: float | None
    unmentioned: tuple[str, ...]


class ModelStaleMemory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reason: str
    identifier: str
    line: str


class ModelPrecisionScore(BaseModel):
    false_memories: tuple[str, ...]
    stale_memories: tuple[ModelStaleMemory, ...]
    identifiers_checked: int


class ModelScoreResult(BaseModel):
    case_id: str
    manifest_digest: str
    ledger_recall: ModelRecallScore
    planted_recall: ModelRecallScore
    mention: ModelMentionScore
    precision: ModelPrecisionScore


class ModelControlCheck(BaseModel):
    control: EnumControl
    passed: bool
    failures: tuple[str, ...]
