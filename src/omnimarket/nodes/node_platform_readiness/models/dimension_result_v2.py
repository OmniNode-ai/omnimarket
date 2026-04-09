# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelDimensionResultV2 — extended per-dimension metadata for platform readiness V2.

Parallel to ModelDimensionResult (V1). Both types coexist for backward compatibility.
V2 adds: sweep_names, check_count, actionable_items, valid_zero, evidence_source, freshness_seconds, raw_detail.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)


class ModelDimensionResultV2(BaseModel):
    """Extended per-dimension result for platform readiness V2 orchestrator.

    valid_zero semantics:
    - valid_zero=True: zero check_count is a real/expected answer (e.g. CI with no failures).
      A PASS status with check_count=0 is legitimate.
    - valid_zero=False: zero check_count means the sweep didn't run or failed silently.
      Callers should set status=WARN and include an actionable_item explaining the gap.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: str
    status: EnumReadinessStatus
    check_count: int = Field(ge=0)
    valid_zero: bool = False
    actionable_items: list[str] = []
    evidence_source: str
    sweep_names: list[str] = []
    freshness_seconds: int | None = Field(default=None, ge=0)
    raw_detail: str = ""
