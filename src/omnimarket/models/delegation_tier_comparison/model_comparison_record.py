# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One comparison record: a (task class, kind, arm) of one eval run (plan section 6.1)."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation_tier_comparison.enum_comparison_arm_kind import (
    EnumComparisonArmKind,
)
from omnimarket.models.delegation_tier_comparison.enum_comparison_baseline_tier import (
    EnumComparisonBaselineTier,
)
from omnimarket.models.delegation_tier_comparison.enum_comparison_cost_basis import (
    EnumComparisonCostBasis,
)
from omnimarket.models.delegation_tier_comparison.enum_comparison_kind import (
    EnumComparisonKind,
)
from omnimarket.models.delegation_tier_comparison.enum_comparison_tier import (
    EnumComparisonTier,
)


class ModelComparisonRecord(BaseModel):
    """Every field is numeric, boolean, a date, an id or an enumerated name. No free text.

    A field is ``None`` only where the fact does not exist for the arm: a Claude arm has no
    saving, a class with no passing baseline has no gap, an arm with no synchronous call has no
    latency.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: UUID
    eval_run_id: str
    prereg_sha256: str
    rubric_id: str
    rubric_version: str
    measured_at: date
    task_class: str
    kind: EnumComparisonKind
    mapped_tier: EnumComparisonTier
    arm_model_key: str
    arm_kind: EnumComparisonArmKind
    arm_host: str | None
    n: int = Field(ge=1)
    accepts_primary: int = Field(ge=0)
    wilson_low_primary: float
    wilson_high_primary: float
    accepts_second: int = Field(ge=0)
    wilson_low_second: float
    wilson_high_second: float
    judge_agreement: float
    judge_kappa: float
    judge_flip_rate: float | None
    passes_rule_primary: bool
    passes_rule_second: bool
    baseline_tier: EnumComparisonBaselineTier
    gap_to_baseline: float | None
    gap_ci_low: float | None
    gap_ci_high: float | None
    latency_p50_ms: float | None
    latency_p90_ms: float | None
    tokens_in_mean: float
    tokens_out_mean: float
    flip_rate: float | None
    cost_per_attempt_usd: float
    cost_basis: EnumComparisonCostBasis
    cost_per_accepted_usd: float | None
    saving_per_task_usd: float | None
    saving_low_usd: float | None
    saving_high_usd: float | None
