# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Determinism failures or the completed calibration report."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.delegation_gate_eval.enum_gate_eval_run_status import (
    EnumGateEvalRunStatus,
)
from omnimarket.events.delegation_gate_eval.model_gate_check_record import (
    ModelGateCheckRecord,
)
from omnimarket.events.delegation_gate_eval.model_gate_item_verdict import (
    ModelGateItemVerdict,
)
from omnimarket.events.delegation_gate_eval.model_gate_rate_row import (
    ModelGateRateRow,
)


class ModelDelegationGateEvalResult(BaseModel):
    """Determinism failures or the completed calibration report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    status: EnumGateEvalRunStatus
    failure_reasons: tuple[str, ...] = ()
    nondeterministic_items: tuple[str, ...] = ()
    undecidable_items: tuple[str, ...] = ()
    unlabelable_count: int = Field(default=0, ge=0)
    rate_rows: tuple[ModelGateRateRow, ...] = ()
    check_records: tuple[ModelGateCheckRecord, ...] = ()
    item_verdicts: tuple[ModelGateItemVerdict, ...] = ()
