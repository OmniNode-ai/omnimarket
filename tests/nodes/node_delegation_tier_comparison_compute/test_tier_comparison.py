# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tier comparison compute builds the plan's section 6.1 records from judged arms."""

from __future__ import annotations

import math
from uuid import NAMESPACE_URL, uuid5

import pytest

from omnimarket.models.delegation_tier_comparison.enum_comparison_baseline_tier import (
    EnumComparisonBaselineTier,
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
from omnimarket.models.delegation_tier_comparison.model_comparison_observation import (
    ModelComparisonObservation,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_record import (
    ModelComparisonRecord,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_result import (
    ModelComparisonResult,
)
from omnimarket.nodes.node_delegation_tier_comparison_compute.handlers.handler_delegation_tier_comparison import (
    HandlerDelegationTierComparison,
)
from tests.nodes.node_delegation_tier_comparison_compute.support import (
    claude_arm,
    local_arm,
    observations,
    request,
)

pytestmark = pytest.mark.unit

# The field list of the plan's section 6.1, spelled here so a stray or missing field fails.
SECTION_6_1_FIELDS = frozenset(
    {
        "record_id", "eval_run_id", "prereg_sha256", "rubric_id", "rubric_version", "measured_at",
        "task_class", "kind", "mapped_tier", "arm_model_key", "arm_kind", "arm_host",
        "n", "accepts_primary", "wilson_low_primary", "wilson_high_primary",
        "accepts_second", "wilson_low_second", "wilson_high_second",
        "judge_agreement", "judge_kappa", "judge_flip_rate",
        "passes_rule_primary", "passes_rule_second",
        "baseline_tier", "gap_to_baseline", "gap_ci_low", "gap_ci_high",
        "latency_p50_ms", "latency_p90_ms", "tokens_in_mean", "tokens_out_mean", "flip_rate",
        "cost_per_attempt_usd", "cost_basis", "cost_per_accepted_usd",
        "saving_per_task_usd", "saving_low_usd", "saving_high_usd",
    }
)  # fmt: skip

HAIKU, SONNET, OPUS = (
    EnumComparisonTier.HAIKU,
    EnumComparisonTier.SONNET,
    EnumComparisonTier.OPUS,
)


def _wilson(k: int, n: int) -> tuple[float, float]:
    z = 1.959963984540054
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def _by_model(
    result_records: tuple[ModelComparisonRecord, ...], key: str
) -> ModelComparisonRecord:
    (match,) = [r for r in result_records if r.arm_model_key == key]
    return match


def _run(
    *arms: ModelComparisonArm, n_required: int = 60, seed: int = 7
) -> ModelComparisonResult:
    return HandlerDelegationTierComparison().handle(
        request(*arms, n_required=n_required, seed=seed)
    )


def test_record_fields_are_exactly_the_section_6_1_list() -> None:
    assert set(ModelComparisonRecord.model_fields) == SECTION_6_1_FIELDS


def test_record_carries_only_numbers_booleans_ids_and_enumerated_names() -> None:
    result = _run(claude_arm(SONNET, 54), local_arm(50))
    for record in result.records:
        for name, value in record.model_dump(mode="python").items():
            assert not isinstance(value, (list, dict)), name
    free_text_free = {
        "eval_run_id", "prereg_sha256", "rubric_id", "rubric_version", "task_class",
        "arm_model_key", "arm_host",
    }  # fmt: skip
    for record in result.records:
        for name, value in record.model_dump(mode="json").items():
            if isinstance(value, str):
                assert name in free_text_free | {
                    "record_id", "measured_at", "kind", "mapped_tier", "arm_kind",
                    "baseline_tier", "cost_basis",
                }, name  # fmt: skip


def test_accept_counts_and_wilson_interval_per_judge() -> None:
    result = _run(claude_arm(SONNET, 48, second_accepted=45))
    record = _by_model(result.records, "claude-sonnet-5-5")
    assert (record.n, record.accepts_primary, record.accepts_second) == (60, 48, 45)
    low, high = _wilson(48, 60)
    assert record.wilson_low_primary == pytest.approx(low)
    assert record.wilson_high_primary == pytest.approx(high)
    low2, high2 = _wilson(45, 60)
    assert record.wilson_low_second == pytest.approx(low2)
    assert record.wilson_high_second == pytest.approx(high2)


def test_pass_rule_needs_rate_and_wilson_low_under_each_judge() -> None:
    result = _run(claude_arm(SONNET, 48, second_accepted=47))
    record = _by_model(result.records, "claude-sonnet-5-5")
    assert record.passes_rule_primary is True
    assert record.passes_rule_second is False
    assert (
        _wilson(48, 60)[0] >= 0.60
    )  # 48 of 60 clears the 0.60 floor, 47 misses the 0.80 rate


def test_pass_rule_wilson_floor_can_fail_with_the_rate_met() -> None:
    # 4 of 5 is a rate of exactly 0.80 but the interval floor is far under 0.60.
    result = _run(claude_arm(SONNET, 4, n=5), n_required=5)
    record = _by_model(result.records, "claude-sonnet-5-5")
    assert record.accepts_primary / record.n >= 0.80
    assert record.wilson_low_primary < 0.60
    assert record.passes_rule_primary is False


def test_baseline_is_the_cheapest_claude_tier_passing_under_both_judges() -> None:
    result = _run(
        claude_arm(HAIKU, 40),
        claude_arm(SONNET, 55),
        claude_arm(OPUS, 58),
        local_arm(50),
    )
    assert {r.baseline_tier for r in result.records} == {
        EnumComparisonBaselineTier.SONNET
    }


def test_cheaper_passing_tier_lowers_the_baseline() -> None:
    result = _run(claude_arm(HAIKU, 55), claude_arm(SONNET, 56))
    assert {r.baseline_tier for r in result.records} == {
        EnumComparisonBaselineTier.HAIKU
    }


def test_tier_above_the_mapped_one_never_becomes_the_baseline() -> None:
    result = _run(
        claude_arm(SONNET, 30, mapped=SONNET), claude_arm(OPUS, 58, mapped=SONNET)
    )
    assert {r.baseline_tier for r in result.records} == {
        EnumComparisonBaselineTier.NONE
    }
    for record in result.records:
        assert record.gap_to_baseline is None
        assert record.gap_ci_low is None
        assert record.saving_per_task_usd is None


def test_a_judge_failing_a_tier_keeps_it_from_being_the_baseline() -> None:
    result = _run(claude_arm(HAIKU, 55, second_accepted=30), claude_arm(SONNET, 55))
    assert {r.baseline_tier for r in result.records} == {
        EnumComparisonBaselineTier.SONNET
    }


def test_paired_gap_to_the_baseline_over_the_same_items() -> None:
    # Baseline accepts items 0..53, local accepts 0..39: they differ on 14 items, every one of
    # them a baseline accept the local arm missed, so the paired gap is exactly 14 / 60.
    result = _run(claude_arm(SONNET, 54), local_arm(40))
    local = _by_model(result.records, "qwen3.8-27b")
    assert local.gap_to_baseline == pytest.approx(14 / 60)
    assert local.gap_to_baseline is not None
    assert local.gap_ci_low is not None
    assert local.gap_ci_high is not None
    assert local.gap_ci_low <= local.gap_to_baseline <= local.gap_ci_high
    baseline = _by_model(result.records, "claude-sonnet-5-5")
    assert baseline.gap_to_baseline == 0.0
    assert (baseline.gap_ci_low, baseline.gap_ci_high) == (0.0, 0.0)


def test_paired_gap_ignores_items_the_two_arms_do_not_share() -> None:
    base = claude_arm(SONNET, 54)
    shifted = tuple(
        ModelComparisonObservation(
            **{**o.model_dump(), "item_id": f"other-{o.item_id}"}
        )
        for o in observations(40, 60)
    )
    stray = local_arm(40).model_copy(update={"observations": shifted})
    result = _run(base, stray)
    assert _by_model(result.records, "qwen3.8-27b").gap_to_baseline is None


def test_bootstrap_interval_is_reproducible_from_its_seed() -> None:
    def interval(seed: int) -> tuple[float | None, float | None]:
        result = _run(claude_arm(SONNET, 54), local_arm(40), seed=seed)
        record = _by_model(result.records, "qwen3.8-27b")
        return record.gap_ci_low, record.gap_ci_high

    assert interval(11) == interval(11)
    assert len({interval(seed) for seed in range(1, 9)}) > 1


def test_saving_per_task_follows_the_section_7_1_formula() -> None:
    # p_L = 50/60, p_F = 54/60, c_F = 0.02 per attempt, c_L = 0.001 per attempt.
    result = _run(claude_arm(SONNET, 54, cost=0.02), local_arm(50, cost=0.001))
    local = _by_model(result.records, "qwen3.8-27b")
    p_l, p_f = 50 / 60, 54 / 60
    assert local.saving_per_task_usd == pytest.approx(p_l * 0.02 / p_f - 0.001)
    low_l, high_l = _wilson(50, 60)
    low_f, high_f = _wilson(54, 60)
    assert local.saving_low_usd == pytest.approx(low_l * 0.02 / high_f - 0.001)
    assert local.saving_high_usd == pytest.approx(high_l * 0.02 / low_f - 0.001)
    assert local.saving_low_usd is not None
    assert local.saving_per_task_usd is not None
    assert local.saving_high_usd is not None
    assert local.saving_low_usd < local.saving_per_task_usd < local.saving_high_usd


def test_claude_arms_carry_no_saving() -> None:
    result = _run(claude_arm(SONNET, 54), local_arm(50))
    claude = _by_model(result.records, "claude-sonnet-5-5")
    assert (
        claude.saving_per_task_usd,
        claude.saving_low_usd,
        claude.saving_high_usd,
    ) == (None, None, None)


def test_latency_percentiles_skip_batched_calls_and_cost_is_per_attempt_and_accepted() -> (
    None
):
    arm = claude_arm(SONNET, 30, n=60, cost=0.02)
    timed = tuple(
        o.model_copy(update={"latency_ms": 100 * (i + 1) if i < 20 else None})
        for i, o in enumerate(arm.observations)
    )
    result = _run(arm.model_copy(update={"observations": timed}))
    record = _by_model(result.records, "claude-sonnet-5-5")
    assert (
        record.latency_p50_ms == 1000.0
    )  # nearest rank over 100..2000: ceil(0.5 * 20) = 10th
    assert record.latency_p90_ms == 1800.0  # ceil(0.9 * 20) = 18th
    assert record.cost_per_attempt_usd == pytest.approx(0.02)
    assert record.cost_per_accepted_usd == pytest.approx(0.02 * 60 / 30)
    assert record.tokens_in_mean == 200.0
    assert record.tokens_out_mean == 50.0


def test_no_accepts_means_no_cost_per_accepted() -> None:
    result = _run(claude_arm(SONNET, 0))
    assert _by_model(result.records, "claude-sonnet-5-5").cost_per_accepted_usd is None


def test_all_latencies_batched_gives_no_latency() -> None:
    result = _run(claude_arm(SONNET, 50, latency=None))
    record = _by_model(result.records, "claude-sonnet-5-5")
    assert (record.latency_p50_ms, record.latency_p90_ms) == (None, None)


def test_judge_agreement_and_kappa() -> None:
    # Primary accepts 0..47, second accepts 0..39: 40 agree-yes, 8 disagree, 12 agree-no.
    result = _run(claude_arm(SONNET, 48, second_accepted=40))
    record = _by_model(result.records, "claude-sonnet-5-5")
    assert record.judge_agreement == pytest.approx(52 / 60)
    p_a, p_b, observed = 48 / 60, 40 / 60, 52 / 60
    expected = p_a * p_b + (1 - p_a) * (1 - p_b)
    assert record.judge_kappa == pytest.approx((observed - expected) / (1 - expected))


def test_identical_single_label_judges_agree_perfectly() -> None:
    result = _run(claude_arm(SONNET, 60))
    record = _by_model(result.records, "claude-sonnet-5-5")
    assert (record.judge_agreement, record.judge_kappa) == (1.0, 1.0)


def test_an_arm_below_its_preregistered_n_is_underpowered_and_never_passes() -> None:
    result = _run(claude_arm(SONNET, 28, n=30), n_required=60)
    record = _by_model(result.records, "claude-sonnet-5-5")
    assert (record.passes_rule_primary, record.passes_rule_second) == (False, False)
    assert record.baseline_tier is EnumComparisonBaselineTier.NONE
    assert [(u.arm_model_key, u.n, u.n_required) for u in result.underpowered] == [
        ("claude-sonnet-5-5", 30, 60)
    ]


def test_record_id_is_a_uuid5_of_run_class_kind_arm_and_host() -> None:
    result = _run(local_arm(50))
    record = result.records[0]
    assert record.record_id == uuid5(
        NAMESPACE_URL,
        "onex:delegation-tier-comparison:eval-2026-10-tiered|summarization|task|qwen3.8-27b|h201",
    )


def test_records_do_not_depend_on_arm_order() -> None:
    arms = (claude_arm(HAIKU, 50), claude_arm(SONNET, 55), local_arm(45))
    forward = HandlerDelegationTierComparison().handle(request(*arms))
    backward = HandlerDelegationTierComparison().handle(request(*reversed(arms)))
    assert forward.records == backward.records


def test_run_provenance_is_copied_onto_every_record() -> None:
    result = _run(claude_arm(SONNET, 54), local_arm(50))
    for record in result.records:
        assert record.eval_run_id == "eval-2026-10-tiered"
        assert record.prereg_sha256 == "a" * 64
        assert (record.rubric_id, record.rubric_version) == ("ev4-blind-v1", "1")
        assert record.measured_at.isoformat() == "2026-10-20"


def test_a_class_with_no_preregistered_n_refuses_the_run() -> None:
    result = HandlerDelegationTierComparison().handle(
        request(
            claude_arm(SONNET, 54, task_class="document"), task_class="summarization"
        )
    )
    assert result.records == ()
    assert [i.refusal for i in result.issues] == [EnumComparisonRefusal.NO_REQUIREMENT]


def test_a_repeated_arm_refuses_the_whole_run() -> None:
    result = _run(claude_arm(SONNET, 54), claude_arm(SONNET, 50))
    assert result.records == ()
    assert [i.refusal for i in result.issues] == [EnumComparisonRefusal.DUPLICATE_ARM]


def test_a_repeated_item_refuses_the_whole_run() -> None:
    arm = claude_arm(SONNET, 54)
    doubled = arm.model_copy(
        update={"observations": arm.observations + arm.observations[:1]}
    )
    result = _run(doubled, local_arm(50))
    assert result.records == ()
    assert [i.refusal for i in result.issues] == [EnumComparisonRefusal.DUPLICATE_ITEM]


def test_an_arm_with_no_items_refuses_the_whole_run() -> None:
    empty = claude_arm(SONNET, 0, n=1).model_copy(update={"observations": ()})
    result = _run(empty)
    assert result.records == ()
    assert [i.refusal for i in result.issues] == [EnumComparisonRefusal.EMPTY_ARM]
