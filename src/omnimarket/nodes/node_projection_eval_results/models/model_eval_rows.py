# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rows the eval-results projection stores (OMN-24171).

``ModelEvalItemRow`` is the whole scored-item record plus the two columns the store adds, so a
column dropped from the table fails a test that walks the record's fields. A trace part, a run's
close and a run's rate are the other three stores. Pure models: no clock, no I/O.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_eval_engine import EnumEvalEngine
from omnimarket.enums.enum_eval_scorer import EnumEvalScorer
from omnimarket.models.eval_item.model_eval_item_scored import ModelEvalItemScored

SIZE_BAND_ALL = "all"
"""The size label of a rate row that counts every band of its run."""


class EnumEvalRunState(StrEnum):
    """Where a run stands, from its close and the first tries held for it.

    open     -- no close has arrived.
    closing  -- the close has arrived and a first try it counts is missing or still unscorable
                for an infrastructure reason; no rate is shown.
    closed   -- every first try is in, none infrastructure-unscorable, over the registered tasks.
    void     -- the close named a cause, or the held tries are not the registered ones.
    live     -- a week of live work: no close, one row per model.
    """

    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    VOID = "void"
    LIVE = "live"


class EnumEvalRefusal(StrEnum):
    """Why one delivery, or part of one, was refused rather than stored."""

    NOT_A_TRY = "not_a_try"
    UNSCORED = "unscored"
    NOT_A_TASK = "not_a_task"
    UNMAPPED_TIER = "unmapped_tier"
    INVALID_ITEM = "invalid_item"
    TRACE_DIGEST_MISMATCH = "trace_digest_mismatch"
    INVALID_EVENT = "invalid_event"


class ModelEvalItemRow(ModelEvalItemScored):
    """One stored scored try: every field of the scored-item record, its key and its outcome digest.

    ``item_key`` is the record's ``event_id``. ``outcome_digest`` is the sha256 of the canonical
    form of every other stored field, so two events that differ in any stored value have
    different digests and, at an equal ``scored_at``, the greater digest stands whatever the
    arrival order.
    """

    item_key: str
    outcome_digest: str = Field(min_length=64, max_length=64)

    @classmethod
    def of(cls, item: ModelEvalItemScored) -> ModelEvalItemRow:
        """The stored row of a scored-item record: its key and its outcome digest filled in."""
        draft = cls.model_validate(
            {
                **item.model_dump(),
                "item_key": str(item.event_id),
                "outcome_digest": "0" * 64,
            }
        )
        digest = hashlib.sha256(draft.digest_material()).hexdigest()
        return draft.model_copy(update={"outcome_digest": digest})

    def digest_material(self) -> bytes:
        """The canonical bytes ``outcome_digest`` covers: every stored field but the digest."""
        return json.dumps(
            self.model_dump(mode="json", exclude={"outcome_digest"}),
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def supersedes(self, standing: ModelEvalItemRow) -> bool:
        """True when this row replaces ``standing``: a later ``scored_at``, or at an equal one the greater digest."""
        if self.scored_at != standing.scored_at:
            return self.scored_at > standing.scored_at
        return self.outcome_digest > standing.outcome_digest


class ModelEvalTraceRow(BaseModel):
    """One numbered part of one scoring's trace, as stored. Never replaced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    item_key: str
    scoring_id: UUID
    part_index: int = Field(ge=0)
    part_count: int = Field(ge=1)
    trace_sha256: str = Field(min_length=64, max_length=64)
    chunk_b64: str
    chunk_sha256: str = Field(min_length=64, max_length=64)
    observed_at: datetime


class ModelEvalRunSequence(BaseModel):
    """One (task, model) pair the run registered, from its close."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    model_id: str
    band: str


class ModelEvalRunRecord(BaseModel):
    """What a run's close told the projection, stored so a close that arrives first is not lost."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    run_id: str
    series: str = ""
    task_sample_digest: str = ""
    closed_at: datetime
    abandoned: bool = False
    void_reason: str = ""
    item_count: int = Field(ge=0)
    sequences: tuple[ModelEvalRunSequence, ...] = ()
    band_usable: dict[str, int] = Field(default_factory=dict)
    band_target: dict[str, int] = Field(default_factory=dict)


class ModelEvalRateRow(BaseModel):
    """One run's rate over first tries for one key and size band, recounted from the items.

    ``size_band`` is S, M, L or ``all``. A live week's row is keyed by (tenant, run, scorer, model)
    alone: its other key columns are empty, its size band is ``all`` and it lists the reviewer
    versions it spans in ``scorer_versions``. ``n`` counts tasks: a model-class unscorable first
    try counts as not passed, an infrastructure-class one is outside ``n`` and counted in
    ``unscorable_infrastructure``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    run_id: str
    scorer: EnumEvalScorer
    task_set_digest: str = ""
    engine: EnumEvalEngine | None = None
    model_id: str
    model_revision: str = ""
    generation_settings_hash: str = ""
    prompt_template_version: str = ""
    scorer_version: str = ""
    context_arm: str = ""
    replicate: int = 0
    size_band: str

    n: int = Field(ge=0)
    passed: int = Field(ge=0)
    unscorable_model: int = Field(ge=0)
    unscorable_infrastructure: int = Field(ge=0)
    wilson_low: float = Field(ge=0.0, le=1.0)
    wilson_high: float = Field(ge=0.0, le=1.0)
    first_scored_at: datetime
    last_scored_at: datetime

    series: str = ""
    run_state: EnumEvalRunState
    void_reason: str = ""
    task_sample_digest: str = ""
    band_usable: dict[str, int] = Field(default_factory=dict)
    band_target: dict[str, int] = Field(default_factory=dict)
    item_count: int = Field(default=0, ge=0)
    scorer_versions: tuple[str, ...] = ()


RATE_KEY_COLUMNS: tuple[str, ...] = (
    "tenant_id",
    "run_id",
    "scorer",
    "task_set_digest",
    "engine",
    "model_id",
    "model_revision",
    "generation_settings_hash",
    "prompt_template_version",
    "scorer_version",
    "context_arm",
    "replicate",
    "size_band",
)
"""The primary key of ``eval_run_rates``; ``engine`` is stored as its value or the empty string."""

__all__ = [
    "RATE_KEY_COLUMNS",
    "SIZE_BAND_ALL",
    "EnumEvalRefusal",
    "EnumEvalRunState",
    "ModelEvalItemRow",
    "ModelEvalRateRow",
    "ModelEvalRunRecord",
    "ModelEvalRunSequence",
    "ModelEvalTraceRow",
]
