# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closed event of a code eval run (OMN-23627).

Exemplar: ``nodes/node_pr_landing_orchestrator/models/``. Published once, by the
reducer's close transition or by the codec's abandoned terminal.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_code_eval_void_reason import EnumCodeEvalVoidReason


class ModelCodeEvalSequenceReading(BaseModel):
    """One task's try sequence for one model: its verdicts in order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_key: str
    model: str
    backend_id: str
    band: str
    verdicts: tuple[str, ...]
    duration_s: float = Field(ge=0)


class ModelCodeEvalControlReading(BaseModel):
    """One task's control: whether the check passes with the held answer and fails at the base."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_key: str
    verdict: str
    held: bool
    could_not_be_run: bool


class ModelCodeEvalRunClosed(BaseModel):
    """``onex.evt.omnimarket.code-eval-run-closed.v1``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    tenant_id: str
    harness_revision: str
    publish_cells: bool
    attempt_budgets: tuple[int, ...]
    digest: str
    closed_at: datetime
    abandoned: bool
    excluded_counts: dict[str, int] = Field(default_factory=dict)
    controls: tuple[ModelCodeEvalControlReading, ...] = ()
    sequences: tuple[ModelCodeEvalSequenceReading, ...] = ()
    #: The cause of a void close (OMN-24173); None when the run closed with every try scored on one revision.
    void_reason: EnumCodeEvalVoidReason | None = None
    #: The model revision each arm's first observed try registered for the run (model -> revision).
    model_revisions: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "ModelCodeEvalControlReading",
    "ModelCodeEvalRunClosed",
    "ModelCodeEvalSequenceReading",
]
