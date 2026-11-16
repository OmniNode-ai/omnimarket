# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed row of one scored try (OMN-24170).

Exemplar: ``omnimarket.events.context_roi``. In this slice the row is the shape the
projection's fold maps its existing sources onto; it is not published on a topic
of its own. Pure model: no clock read, no I/O.

Identity. ``event_id`` is the uuid5 of eight fields (scorer, run_id, task_id,
engine, model_id, context_arm, replicate, try_ordinal), so two rows of the same
try have the same id and an infrastructure retry, which keeps the ordinal, replaces
the earlier unscorable outcome. ``scoring_id`` is the uuid5 of ``event_id``,
``scored_at`` and the sha256 of that scoring's trace, so each scoring and its trace
share one id.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.enums.enum_eval_engine import EnumEvalEngine
from omnimarket.enums.enum_eval_lane import EnumEvalLane
from omnimarket.enums.enum_eval_scorer import EnumEvalScorer
from omnimarket.enums.enum_eval_step_verdict import (
    EnumEvalStepVerdict,
    EnumEvalStepVerdictSource,
)
from omnimarket.enums.enum_eval_unscorable_reason import EnumEvalUnscorableReason
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.eval_item.model_eval_field_score import ModelEvalFieldScore
from omnimarket.models.model_factor_arm import EnumArmLabel

CONTEXT_ARM_UNKNOWN = "unknown"
"""The label of live work whose arm was not recorded: it resolves to no factor set."""

CONTEXT_ARM_VOCABULARY = frozenset(label.value for label in EnumArmLabel) | {
    CONTEXT_ARM_UNKNOWN
}
"""Every label ``ModelFactorArm`` knows, plus ``unknown``. The baseline is ``off``."""

_EVENT_ID_NAMESPACE = uuid5(NAMESPACE_URL, "urn:omnimarket:eval-item:event-id:v1")
_SCORING_ID_NAMESPACE = uuid5(NAMESPACE_URL, "urn:omnimarket:eval-item:scoring-id:v1")

_IDENTITY_FIELDS = (
    "scorer",
    "run_id",
    "task_id",
    "engine",
    "model_id",
    "context_arm",
    "replicate",
    "try_ordinal",
)
_DEFAULTED = frozenset({"replicate", "try_ordinal"})


def _plain(value: Any) -> Any:
    return getattr(value, "value", value)


def event_id_of(
    *,
    scorer: str,
    run_id: str,
    task_id: str,
    engine: str,
    model_id: str,
    context_arm: str,
    replicate: int,
    try_ordinal: int,
) -> UUID:
    """The uuid5 of the eight identity fields, joined as a JSON list so no field can collide with another."""
    material = json.dumps(
        [
            scorer,
            run_id,
            task_id,
            engine,
            model_id,
            context_arm,
            replicate,
            try_ordinal,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return uuid5(_EVENT_ID_NAMESPACE, material)


def scoring_id_of(*, event_id: UUID, scored_at: datetime, trace_sha256: str) -> UUID:
    """The uuid5 shared by one scoring and its trace."""
    return uuid5(
        _SCORING_ID_NAMESPACE,
        json.dumps(
            [str(event_id), scored_at.isoformat(), trace_sha256],
            ensure_ascii=True,
            separators=(",", ":"),
        ),
    )


class ModelEvalItemScored(BaseModel):
    """One scored try: who answered which task, how it was scored, and the result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # identity
    event_id: UUID
    tenant_id: str
    run_id: str
    experiment_id: str = ""
    replicate: int = Field(
        default=0,
        ge=0,
        le=1,
        description="0 in this slice; the field stays so the identity does not change when the experiment arrives.",
    )
    try_ordinal: int = Field(
        default=1,
        ge=1,
        description="1 for the model's first attempt; an infrastructure retry keeps it with a later scored_at.",
    )

    # the task
    task_set_id: str
    task_set_digest: str
    task_id: str
    task_type: str
    size_band: EnumSizeBand

    # who answered
    engine: EnumEvalEngine
    model_id: str
    model_revision: str = ""
    generation_settings_hash: str = ""
    prompt_template_version: str = ""

    # the context
    context_arm: str
    context_pack_hash: str = ""
    pack_profile_hash: str = ""
    prompt_tokens: int = Field(default=0, ge=0)

    # the score
    scorer: EnumEvalScorer
    scorer_version: str = ""
    field_scores: tuple[ModelEvalFieldScore, ...] = ()
    passed: bool | None
    unscorable_reason: EnumEvalUnscorableReason | None = None
    step_verdict: EnumEvalStepVerdict = EnumEvalStepVerdict.NONE
    step_verdict_source: EnumEvalStepVerdictSource = (
        EnumEvalStepVerdictSource.DETERMINISTIC
    )

    # time and provenance
    scored_at: datetime
    trace_sha256: str = ""
    scoring_id: UUID
    lane: EnumEvalLane = EnumEvalLane.DEV
    host: str = ""
    install_revision: str = ""

    @model_validator(mode="before")
    @classmethod
    def _derive_ids(cls, data: Any) -> Any:
        """Set ``event_id`` and ``scoring_id`` from their inputs; refuse a supplied id that disagrees."""
        if not isinstance(data, dict):
            return data
        needed = (*(n for n in _IDENTITY_FIELDS if n not in _DEFAULTED), "scored_at")
        if not all(name in data for name in needed):
            return data
        scored_at = data["scored_at"]
        if isinstance(scored_at, str):
            try:
                scored_at = datetime.fromisoformat(scored_at)
            except ValueError:
                return data
        if not isinstance(scored_at, datetime):
            return data
        try:
            event_id = event_id_of(
                scorer=str(_plain(data["scorer"])),
                run_id=str(data["run_id"]),
                task_id=str(data["task_id"]),
                engine=str(_plain(data["engine"])),
                model_id=str(data["model_id"]),
                context_arm=str(_plain(data["context_arm"])),
                replicate=int(data.get("replicate", 0)),
                try_ordinal=int(data.get("try_ordinal", 1)),
            )
        except (TypeError, ValueError):
            return data
        scoring_id = scoring_id_of(
            event_id=event_id,
            scored_at=scored_at,
            trace_sha256=str(data.get("trace_sha256", "")),
        )
        derived = {**data}
        for name, expected in (("event_id", event_id), ("scoring_id", scoring_id)):
            supplied = data.get(name)
            if supplied is not None and str(supplied) != str(expected):
                raise ValueError(
                    f"{name} {supplied} is not the uuid5 of its inputs ({expected})"
                )
            derived[name] = expected
        return derived

    @field_validator("context_arm")
    @classmethod
    def _context_arm_is_in_the_vocabulary(cls, value: str) -> str:
        if value not in CONTEXT_ARM_VOCABULARY:
            raise ValueError(
                f"context_arm {value!r} is outside the vocabulary "
                f"({', '.join(sorted(CONTEXT_ARM_VOCABULARY))})"
            )
        return value

    @model_validator(mode="after")
    def _unscorable_needs_a_reason(self) -> ModelEvalItemScored:
        if self.passed is None and self.unscorable_reason is None:
            raise ValueError("unscorable_reason is required while passed is null")
        return self

    @model_validator(mode="after")
    def _unknown_arm_only_for_the_reviewer_model(self) -> ModelEvalItemScored:
        if (
            self.context_arm == CONTEXT_ARM_UNKNOWN
            and self.scorer is not EnumEvalScorer.REVIEWER_MODEL
        ):
            raise ValueError(
                "context_arm unknown is accepted only with scorer reviewer_model"
            )
        return self


__all__ = [
    "CONTEXT_ARM_UNKNOWN",
    "CONTEXT_ARM_VOCABULARY",
    "ModelEvalItemScored",
    "event_id_of",
    "scoring_id_of",
]
