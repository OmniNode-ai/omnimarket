# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One replay verdict and its per-check evidence."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.events.delegation_gate_eval.enum_gate_verdict import (
    EnumGateVerdict,
)
from omnimarket.events.delegation_gate_eval.model_gate_check_skip import (
    ModelGateCheckSkip,
)


class ModelGateReplayVerdict(BaseModel):
    """One replay verdict and its per-check evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumGateVerdict
    deciding_check: str | None = None
    checks_refused: tuple[str, ...] = ()
    checks_skipped: tuple[ModelGateCheckSkip, ...] = ()
