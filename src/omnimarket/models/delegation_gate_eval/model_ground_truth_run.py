# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One delegated run with the gate's verdict and independent evidence (OMN-19787)."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.delegation_gate_eval.enum_gate_verdict import EnumGateVerdict
from omnimarket.models.delegation_gate_eval.model_checklist_score import (
    ModelChecklistScore,
)
from omnimarket.models.delegation_gate_eval.model_hidden_test_outcome import (
    ModelHiddenTestOutcome,
)


class ModelGroundTruthRun(BaseModel):
    """Exactly one evidence kind: a hidden test or a scored checklist."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    task_class: str = Field(min_length=1)
    stratum: str = Field(min_length=1)
    delegated_model: str = Field(min_length=1)
    prompt_text: str
    answer_text: str | None = None
    gate_verdict: EnumGateVerdict | None = None
    gate_deciding_check: str | None = None
    hidden_test: ModelHiddenTestOutcome | None = None
    checklist: ModelChecklistScore | None = None

    @model_validator(mode="after")
    def _exactly_one_evidence_kind(self) -> Self:
        if (self.hidden_test is None) == (self.checklist is None):
            raise ValueError("supply exactly one of hidden_test or checklist")
        return self
