# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Labelled evidence supplied for deterministic gate replay."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegation_gate_eval_compute.models.enum_gate_eval_label import (
    EnumGateEvalLabel,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.enum_gate_verdict import (
    EnumGateVerdict,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.model_gate_check_skip import (
    ModelGateCheckSkip,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.model_gate_execution_result import (
    ModelGateExecutionResult,
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
