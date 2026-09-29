# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One replay verdict and its per-check evidence."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegation_gate_eval_compute.models.enum_gate_verdict import (
    EnumGateVerdict,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.model_gate_check_skip import (
    ModelGateCheckSkip,
)


class ModelGateReplayVerdict(BaseModel):
    """One replay verdict and its per-check evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumGateVerdict
    deciding_check: str | None = None
    checks_refused: tuple[str, ...] = ()
    checks_skipped: tuple[ModelGateCheckSkip, ...] = ()
