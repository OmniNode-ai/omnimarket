# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Labelled evidence supplied for deterministic gate replay."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.events.delegation_gate_eval.enum_gate_eval_label import (
    EnumGateEvalLabel,
)
from omnimarket.events.delegation_gate_eval.enum_gate_verdict import (
    EnumGateVerdict,
)
from omnimarket.events.delegation_gate_eval.model_gate_check_skip import (
    ModelGateCheckSkip,
)
from omnimarket.events.delegation_gate_eval.model_gate_execution_result import (
    ModelGateExecutionResult,
)
from omnimarket.models.delegation.wire.model_attempt_rubric_verdict import (
    ModelAttemptRubricVerdict,
)


class ModelGateEvalItem(BaseModel):
    """Labelled evidence supplied for deterministic gate replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str
    task_class: str
    stratum: str
    label: EnumGateEvalLabel
    prompt_text: str
    recorded_answer: str | None = None
    recorded_verdict: EnumGateVerdict | None = None
    recorded_deciding_check: str | None = None
    requires_execution: bool = False
    execution_result: ModelGateExecutionResult | None = None
    checks_declared: tuple[str, ...] = ()
    recorded_checks_refused: tuple[str, ...] = ()
    recorded_checks_skipped: tuple[ModelGateCheckSkip, ...] = ()

    rubric_verdict: ModelAttemptRubricVerdict | None = None
