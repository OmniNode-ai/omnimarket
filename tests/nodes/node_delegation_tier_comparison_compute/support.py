# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for the tier comparison tests."""

from __future__ import annotations

from datetime import date

from omnimarket.models.delegation_tier_comparison.enum_comparison_arm_kind import (
    EnumComparisonArmKind,
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
from omnimarket.models.delegation_tier_comparison.model_comparison_arm import (
    ModelComparisonArm,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_class_requirement import (
    ModelComparisonClassRequirement,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_observation import (
    ModelComparisonObservation,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_request import (
    ModelComparisonRequest,
)

CLASS = "summarization"


def observations(
    accepted: int,
    n: int,
    *,
    second_accepted: int | None = None,
    cost: float = 0.01,
    latency: int | None = 1000,
    tokens_in: int = 200,
    tokens_out: int = 50,
) -> tuple[ModelComparisonObservation, ...]:
    """n items; the first ``accepted`` pass the primary judge, the first ``second_accepted`` the second."""
    second = accepted if second_accepted is None else second_accepted
    return tuple(
        ModelComparisonObservation(
            item_id=f"item-{i:03d}",
            accepted_primary=i < accepted,
            accepted_second=i < second,
            latency_ms=latency,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
        )
        for i in range(n)
    )


def claude_arm(
    tier: EnumComparisonTier,
    accepted: int,
    n: int = 60,
    *,
    mapped: EnumComparisonTier = EnumComparisonTier.SONNET,
    second_accepted: int | None = None,
    cost: float = 0.02,
    latency: int | None = 1000,
    task_class: str = CLASS,
) -> ModelComparisonArm:
    return ModelComparisonArm(
        task_class=task_class,
        kind=EnumComparisonKind.TASK,
        mapped_tier=mapped,
        arm_model_key=f"claude-{tier.value}-5-5",
        arm_kind=EnumComparisonArmKind.CLAUDE,
        claude_tier=tier,
        cost_basis=EnumComparisonCostBasis.LIST,
        observations=observations(
            accepted, n, second_accepted=second_accepted, cost=cost, latency=latency
        ),
    )


def local_arm(
    accepted: int,
    n: int = 60,
    *,
    mapped: EnumComparisonTier = EnumComparisonTier.SONNET,
    cost: float = 0.001,
    host: str = "h201",
    task_class: str = CLASS,
) -> ModelComparisonArm:
    return ModelComparisonArm(
        task_class=task_class,
        kind=EnumComparisonKind.TASK,
        mapped_tier=mapped,
        arm_model_key="qwen3.8-27b",
        arm_kind=EnumComparisonArmKind.LOCAL,
        arm_host=host,
        cost_basis=EnumComparisonCostBasis.ESTIMATED_LOCAL,
        observations=observations(accepted, n, cost=cost),
    )


def request(
    *arms: ModelComparisonArm,
    n_required: int = 60,
    resamples: int = 2000,
    seed: int = 7,
    task_class: str = CLASS,
) -> ModelComparisonRequest:
    return ModelComparisonRequest(
        eval_run_id="eval-2026-10-tiered",
        prereg_sha256="a" * 64,
        rubric_id="ev4-blind-v1",
        rubric_version="1",
        measured_at=date(2026, 10, 20),
        requirements=(
            ModelComparisonClassRequirement(
                task_class=task_class,
                kind=EnumComparisonKind.TASK,
                n_required=n_required,
            ),
        ),
        arms=arms,
        bootstrap_resamples=resamples,
        bootstrap_seed=seed,
    )
