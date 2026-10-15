# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the tiered delegation comparison records of one eval run (OMN-20968, plan section 6.1).

``handle`` takes every arm of every task class with its judged items and returns one record per
(task class, kind, arm). It reads no file, calls no model, reads no clock and holds no price: the
cost of each attempt arrives on the item. The pass rule, the baseline and the paired gap are the
plan's section 2.4; the saving per task is section 7.1,
``p_L * c_F / p_F - c_L`` with its range from the Wilson bounds.

The result is all of the run's records or none of them. A class with no pre-registered n, a
repeated arm, a repeated item, an arm with no items or arms of one class mapped to different tiers
refuses the whole run, so a published snapshot never mixes a sound class with an unsound one.
"""

from __future__ import annotations

from collections import defaultdict
from uuid import NAMESPACE_URL, UUID, uuid5

from omnimarket.models.delegation_tier_comparison.enum_comparison_arm_kind import (
    EnumComparisonArmKind,
)
from omnimarket.models.delegation_tier_comparison.enum_comparison_baseline_tier import (
    EnumComparisonBaselineTier,
)
from omnimarket.models.delegation_tier_comparison.enum_comparison_kind import (
    EnumComparisonKind,
)
from omnimarket.models.delegation_tier_comparison.enum_comparison_refusal import (
    EnumComparisonRefusal,
)
from omnimarket.models.delegation_tier_comparison.enum_comparison_tier import (
    EnumComparisonTier,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_arm import (
    ModelComparisonArm,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_issue import (
    ModelComparisonIssue,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_record import (
    ModelComparisonRecord,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_request import (
    ModelComparisonRequest,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_result import (
    ModelComparisonResult,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_underpowered import (
    ModelComparisonUnderpowered,
)
from omnimarket.nodes.node_delegation_tier_comparison_compute.handlers.comparison_stats import (
    cohen_kappa,
    nearest_rank_percentile,
    paired_bootstrap_interval,
    wilson_interval,
)

_RECORD_ID_PREFIX = "onex:delegation-tier-comparison:"

_ClassKey = tuple[str, EnumComparisonKind]


def _arm_key(arm: ModelComparisonArm) -> tuple[str, str, str, str]:
    return (arm.task_class, arm.kind.value, arm.arm_model_key, arm.arm_host or "")


def _record_id(request: ModelComparisonRequest, arm: ModelComparisonArm) -> UUID:
    name = "|".join(
        (
            request.eval_run_id,
            arm.task_class,
            arm.kind.value,
            arm.arm_model_key,
            arm.arm_host or "",
        )
    )
    return uuid5(NAMESPACE_URL, _RECORD_ID_PREFIX + name)


def _refusals(request: ModelComparisonRequest) -> list[ModelComparisonIssue]:
    issues: list[ModelComparisonIssue] = []
    required = {(r.task_class, r.kind) for r in request.requirements}
    seen: set[tuple[str, str, str, str]] = set()
    mapped: dict[_ClassKey, set[str]] = defaultdict(set)
    for arm in sorted(request.arms, key=_arm_key):
        key = _arm_key(arm)
        if (arm.task_class, arm.kind) not in required:
            issues.append(
                ModelComparisonIssue(
                    refusal=EnumComparisonRefusal.NO_REQUIREMENT,
                    task_class=arm.task_class,
                    arm_model_key=arm.arm_model_key,
                )
            )
        if key in seen:
            issues.append(
                ModelComparisonIssue(
                    refusal=EnumComparisonRefusal.DUPLICATE_ARM,
                    task_class=arm.task_class,
                    arm_model_key=arm.arm_model_key,
                )
            )
        seen.add(key)
        if not arm.observations:
            issues.append(
                ModelComparisonIssue(
                    refusal=EnumComparisonRefusal.EMPTY_ARM,
                    task_class=arm.task_class,
                    arm_model_key=arm.arm_model_key,
                )
            )
        ids = [o.item_id for o in arm.observations]
        if len(ids) != len(set(ids)):
            issues.append(
                ModelComparisonIssue(
                    refusal=EnumComparisonRefusal.DUPLICATE_ITEM,
                    task_class=arm.task_class,
                    arm_model_key=arm.arm_model_key,
                )
            )
        mapped[(arm.task_class, arm.kind)].add(arm.mapped_tier.value)
    issues.extend(
        ModelComparisonIssue(
            refusal=EnumComparisonRefusal.MIXED_MAPPED_TIER, task_class=task_class
        )
        for (task_class, _), tiers in sorted(mapped.items())
        if len(tiers) > 1
    )
    return issues


class _ArmFacts:
    """The counts and rates of one arm, computed once and read by the baseline and the gap."""

    def __init__(
        self, arm: ModelComparisonArm, request: ModelComparisonRequest, n_required: int
    ) -> None:
        obs = arm.observations
        self.arm = arm
        self.n = len(obs)
        self.accepts_primary = sum(o.accepted_primary for o in obs)
        self.accepts_second = sum(o.accepted_second for o in obs)
        self.wilson_primary = wilson_interval(self.accepts_primary, self.n)
        self.wilson_second = wilson_interval(self.accepts_second, self.n)
        self.rate_primary = self.accepts_primary / self.n
        self.rate_second = self.accepts_second / self.n
        self.mean_cost = sum(o.cost_usd for o in obs) / self.n
        self.underpowered = self.n < n_required
        self.passes_primary = self._passes(
            self.rate_primary, self.wilson_primary[0], request
        )
        self.passes_second = self._passes(
            self.rate_second, self.wilson_second[0], request
        )
        self.by_item = {o.item_id: o for o in obs}

    def _passes(
        self, rate: float, wilson_low: float, request: ModelComparisonRequest
    ) -> bool:
        return (
            not self.underpowered
            and rate >= request.accept_rate_min
            and wilson_low >= request.wilson_low_min
        )

    @property
    def tier_rank(self) -> int:
        tier = self.arm.claude_tier
        return tier.rank if tier is not None else len(EnumComparisonTier)

    @property
    def passes_both(self) -> bool:
        return self.passes_primary and self.passes_second


def _baseline(facts: list[_ArmFacts]) -> _ArmFacts | None:
    """The cheapest Claude tier, up to the mapped one, that passes under both judges."""
    passing = [
        f
        for f in facts
        if f.arm.claude_tier is not None
        and f.tier_rank <= f.arm.mapped_tier.rank
        and f.passes_both
    ]
    if not passing:
        return None
    return min(passing, key=lambda f: (f.tier_rank, f.arm.arm_model_key))


def _baseline_tier(baseline: _ArmFacts | None) -> EnumComparisonBaselineTier:
    if baseline is None or baseline.arm.claude_tier is None:
        return EnumComparisonBaselineTier.NONE
    return EnumComparisonBaselineTier(baseline.arm.claude_tier.value)


def _gap(
    facts: _ArmFacts, baseline: _ArmFacts, request: ModelComparisonRequest
) -> tuple[float, float, float] | None:
    shared = sorted(set(facts.by_item) & set(baseline.by_item))
    if not shared:
        return None
    differences = [
        int(baseline.by_item[i].accepted_primary)
        - int(facts.by_item[i].accepted_primary)
        for i in shared
    ]
    gap = sum(differences) / len(differences)
    if not any(differences):
        return gap, 0.0, 0.0
    low, high = paired_bootstrap_interval(
        differences,
        resamples=request.bootstrap_resamples,
        seed=request.bootstrap_seed,
    )
    return gap, low, high


def _saving(facts: _ArmFacts, baseline: _ArmFacts) -> tuple[float, float, float] | None:
    """Section 7.1: p_L * c_F / p_F - c_L, with the range from the Wilson bounds."""
    if (
        facts.arm.arm_kind is not EnumComparisonArmKind.LOCAL
        or baseline.rate_primary == 0
    ):
        return None
    c_f, c_l = baseline.mean_cost, facts.mean_cost
    local_low, local_high = facts.wilson_primary
    base_low, base_high = baseline.wilson_primary
    if base_low == 0:
        return None
    return (
        facts.rate_primary * c_f / baseline.rate_primary - c_l,
        local_low * c_f / base_high - c_l,
        local_high * c_f / base_low - c_l,
    )


def _record(
    request: ModelComparisonRequest, facts: _ArmFacts, baseline: _ArmFacts | None
) -> ModelComparisonRecord:
    arm = facts.arm
    obs = arm.observations
    gap = _gap(facts, baseline, request) if baseline is not None else None
    saving = _saving(facts, baseline) if baseline is not None else None
    latencies = [o.latency_ms for o in obs if o.latency_ms is not None]
    pairs = [(o.accepted_primary, o.accepted_second) for o in obs]
    return ModelComparisonRecord(
        record_id=_record_id(request, arm),
        eval_run_id=request.eval_run_id,
        prereg_sha256=request.prereg_sha256,
        rubric_id=request.rubric_id,
        rubric_version=request.rubric_version,
        measured_at=request.measured_at,
        task_class=arm.task_class,
        kind=arm.kind,
        mapped_tier=arm.mapped_tier,
        arm_model_key=arm.arm_model_key,
        arm_kind=arm.arm_kind,
        arm_host=arm.arm_host,
        n=facts.n,
        accepts_primary=facts.accepts_primary,
        wilson_low_primary=facts.wilson_primary[0],
        wilson_high_primary=facts.wilson_primary[1],
        accepts_second=facts.accepts_second,
        wilson_low_second=facts.wilson_second[0],
        wilson_high_second=facts.wilson_second[1],
        judge_agreement=sum(a == b for a, b in pairs) / facts.n,
        judge_kappa=cohen_kappa(pairs),
        judge_flip_rate=arm.judge_flip_rate,
        passes_rule_primary=facts.passes_primary,
        passes_rule_second=facts.passes_second,
        baseline_tier=_baseline_tier(baseline),
        gap_to_baseline=gap[0] if gap else None,
        gap_ci_low=gap[1] if gap else None,
        gap_ci_high=gap[2] if gap else None,
        latency_p50_ms=nearest_rank_percentile(latencies, 0.5),
        latency_p90_ms=nearest_rank_percentile(latencies, 0.9),
        tokens_in_mean=sum(o.tokens_in for o in obs) / facts.n,
        tokens_out_mean=sum(o.tokens_out for o in obs) / facts.n,
        flip_rate=arm.flip_rate,
        cost_per_attempt_usd=facts.mean_cost,
        cost_basis=arm.cost_basis,
        cost_per_accepted_usd=(
            facts.mean_cost * facts.n / facts.accepts_primary
            if facts.accepts_primary
            else None
        ),
        saving_per_task_usd=saving[0] if saving else None,
        saving_low_usd=saving[1] if saving else None,
        saving_high_usd=saving[2] if saving else None,
    )


class HandlerDelegationTierComparison:
    """Turn the judged arms of one eval run into its comparison records."""

    def handle(self, request: ModelComparisonRequest) -> ModelComparisonResult:
        issues = _refusals(request)
        if issues:
            return ModelComparisonResult(issues=tuple(issues))
        n_required = {
            (r.task_class, r.kind): r.n_required for r in request.requirements
        }
        groups: dict[_ClassKey, list[_ArmFacts]] = defaultdict(list)
        for arm in sorted(request.arms, key=_arm_key):
            key = (arm.task_class, arm.kind)
            groups[key].append(_ArmFacts(arm, request, n_required[key]))
        records: list[ModelComparisonRecord] = []
        underpowered: list[ModelComparisonUnderpowered] = []
        for key in sorted(groups, key=lambda k: (k[0], k[1].value)):
            group = groups[key]
            baseline = _baseline(group)
            for facts in group:
                records.append(_record(request, facts, baseline))
                if facts.underpowered:
                    underpowered.append(
                        ModelComparisonUnderpowered(
                            task_class=key[0],
                            arm_model_key=facts.arm.arm_model_key,
                            n=facts.n,
                            n_required=n_required[key],
                        )
                    )
        return ModelComparisonResult(
            records=tuple(records), underpowered=tuple(underpowered)
        )
