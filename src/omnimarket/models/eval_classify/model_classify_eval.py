# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of node_classify_eval_compute: build the labelled set, or score engine answers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.eval_classify.enum_classify import (
    EnumClassifyEvalFailure,
    EnumClassifyOperation,
    EnumIntegrationClass,
)
from omnimarket.models.eval_classify.model_classify_task import ModelClassifyEvalTask


class ModelClassifyAnswer(BaseModel):
    """One engine's raw reply for one task on one input variant, with its wall time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: str = Field(min_length=1)
    input_variant: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    reply: str
    seconds: float = Field(ge=0)


class ModelClassifyEvalRequest(BaseModel):
    """``build_set`` carries ``ledger_text``; ``score_answers`` carries ``tasks`` and ``answers``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumClassifyOperation
    suite_id: str = Field(min_length=1)
    ledger_text: str | None = None
    tasks: tuple[ModelClassifyEvalTask, ...] = ()
    answers: tuple[ModelClassifyAnswer, ...] = ()

    @model_validator(mode="after")
    def _payload_matches_operation(self) -> ModelClassifyEvalRequest:
        if (
            self.operation is EnumClassifyOperation.BUILD_SET
            and self.ledger_text is None
        ):
            raise ValueError("build_set carries ledger_text")
        return self


class ModelClassifyClassScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: EnumIntegrationClass
    n: int = Field(ge=0)
    correct: int = Field(ge=0)
    accuracy: float | None


class ModelClassifyConfusionCell(BaseModel):
    """How many replies for tasks of ``label`` named ``predicted`` (None: the reply named no class)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: EnumIntegrationClass
    predicted: EnumIntegrationClass | None
    count: int = Field(ge=1)


class ModelClassifyEngineScore(BaseModel):
    """One engine on one input variant: accuracy overall and per class, replies with no class, mean wall time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: str
    input_variant: str
    n: int = Field(ge=0)
    correct: int = Field(ge=0)
    unparsed: int = Field(ge=0)
    accuracy: float | None
    mean_seconds: float | None
    by_class: tuple[ModelClassifyClassScore, ...]
    confusion: tuple[ModelClassifyConfusionCell, ...]


class ModelClassifyClassCount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: EnumIntegrationClass
    available: int = Field(ge=0)
    drawn: int = Field(ge=0)


class ModelClassifyEvalResult(BaseModel):
    """``failure`` is set instead of a result, never beside one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    suite_id: str
    operation: EnumClassifyOperation
    failure: EnumClassifyEvalFailure | None = None
    detail: str = ""
    rubric: str = ""
    tasks: tuple[ModelClassifyEvalTask, ...] = ()
    class_counts: tuple[ModelClassifyClassCount, ...] = ()
    ambiguous_excluded: int = Field(default=0, ge=0)
    scores: tuple[ModelClassifyEngineScore, ...] = ()


__all__ = [
    "ModelClassifyAnswer",
    "ModelClassifyClassCount",
    "ModelClassifyClassScore",
    "ModelClassifyConfusionCell",
    "ModelClassifyEngineScore",
    "ModelClassifyEvalRequest",
    "ModelClassifyEvalResult",
]
