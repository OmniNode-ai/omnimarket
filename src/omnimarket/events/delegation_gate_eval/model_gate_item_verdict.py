# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One item's recorded verdict beside its (agreed) replayed verdict."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.delegation_gate_eval.enum_gate_eval_label import (
    EnumGateEvalLabel,
)
from omnimarket.events.delegation_gate_eval.enum_gate_verdict import (
    EnumGateVerdict,
)
from omnimarket.events.delegation_gate_eval.model_gate_replay_verdict import (
    ModelGateReplayVerdict,
)


class ModelGateItemVerdict(BaseModel):
    """One item's recorded verdict beside its (agreed) replayed verdict (OMN-19793)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str
    task_class: str
    stratum: str
    label: EnumGateEvalLabel
    recorded_verdict: EnumGateVerdict | None
    recorded_deciding_check: str | None
    replayed: ModelGateReplayVerdict
    replay_count: int = Field(ge=1)
