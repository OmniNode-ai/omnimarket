# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The GLM allowance compute: what is left of the window and the week, and the chain it leaves.

The policy below is synthetic: the real tier, caps, window and rates are deployment facts that live in the
private routing overlay and arrive in the request. Nothing here is a value the operator's plan has.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.enums.enum_glm_allowance import (
    EnumGlmAllowanceVerdict,
    EnumGlmRefusalScope,
    EnumGlmSkipReason,
)
from omnimarket.models.glm_allowance.glm_credit_math import (
    credits_for_usage,
    peak_factor,
    week_start,
)
from omnimarket.models.glm_allowance.model_glm_allowance_policy import (
    ModelGlmAllowancePolicy,
)
from omnimarket.models.glm_allowance.model_glm_usage_record import (
    ModelGlmRefusalRecord,
    ModelGlmUsageRecord,
)
from omnimarket.nodes.node_glm_allowance_compute import (
    HandlerGlmAllowance,
    NodeGlmAllowanceCompute,
)
from omnimarket.nodes.node_glm_allowance_compute.models.model_glm_allowance import (
    ModelGlmAllowanceRequest,
    ModelGlmAllowanceResult,
    ModelGlmChainRung,
)

NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_glm_allowance_compute"
)

# A Wednesday, 10:00 UTC = 18:00 UTC+8, just outside the synthetic peak window (14:00-18:00 UTC+8).
NOW = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
ANCHOR = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)  # a Monday


def policy(**over: Any) -> ModelGlmAllowancePolicy:
    base: dict[str, Any] = {
        "provider_id": "plan-x",
        "window_hours": 5,
        "window_credits": 1000.0,
        "weekly_credits": 5000.0,
        "week_anchor": ANCHOR,
        "window_reserve_fraction": 0.1,
        "week_reserve_fraction": 0.1,
        "prefer_min_remaining_fraction": 0.5,
        "call_reserve_credits": 20.0,
        "rate_cooldown_s": 120,
        "credit_divisor": 10000.0,
        "credit_rates": [
            {
                "model": "m-big",
                "input_rate": 10.0,
                "cached_input_rate": 2.0,
                "output_rate": 30.0,
            },
            {
                "model": "m-small",
                "input_rate": 3.0,
                "cached_input_rate": 1.0,
                "output_rate": 10.0,
            },
        ],
        "peak": {
            "utc_offset_hours": 8,
            "weekdays": [0, 1, 2, 3, 4],
            "start_hour": 14,
            "end_hour": 18,
            "off_peak_factor": 0.5,
        },
        "approved_classes": [
            {"task_class": "document", "min_budget_s": None},
            {"task_class": "code_review", "min_budget_s": 600},
        ],
        "cap_codes": [
            {"code": "W1", "scope": "window"},
            {"code": "K1", "scope": "week"},
        ],
    }
    base.update(over)
    return ModelGlmAllowancePolicy.model_validate(base)


def spend(
    credits: float, *, ago: timedelta = timedelta(hours=1), run: str = "r"
) -> ModelGlmUsageRecord:
    return ModelGlmUsageRecord(
        run_id=f"{run}-{credits}-{ago}",
        started_at=NOW - ago,
        model="m-big",
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        credits=credits,
    )


CHAIN = (
    ModelGlmChainRung(name="local", is_glm=False, cost_rank=0),
    ModelGlmChainRung(name="glm_flash", is_glm=True, cost_rank=1),
    ModelGlmChainRung(name="glm", is_glm=True, cost_rank=2),
    ModelGlmChainRung(name="haiku", is_glm=False, cost_rank=3),
    ModelGlmChainRung(name="sonnet", is_glm=False, cost_rank=4),
)


def request(**over: Any) -> ModelGlmAllowanceRequest:
    base: dict[str, Any] = {
        "now": NOW,
        "policy": policy(),
        "usage": (),
        "refusals": (),
        "task_class": "document",
        "budget_s": 180,
        "estimated_credits": None,
        "chain": CHAIN,
    }
    base.update(over)
    return ModelGlmAllowanceRequest.model_validate(base)


def decide(**over: Any) -> ModelGlmAllowanceResult:
    return HandlerGlmAllowance().handle(request(**over))


# --- credit arithmetic ----------------------------------------------------------------------------------


def test_credits_follow_the_tokens_the_rates_and_the_divisor() -> None:
    off_peak = ModelGlmUsageRecord(
        run_id="a",
        started_at=NOW,
        model="m-big",
        input_tokens=10_000,
        cached_input_tokens=5_000,
        output_tokens=2_000,
        credits=None,
    )
    # (10000*10 + 5000*2 + 2000*30) / 10000 = 17.0, halved off-peak.
    assert credits_for_usage(policy(), off_peak) == pytest.approx(8.5)


def test_peak_hours_cost_full_rate_and_other_hours_the_discount() -> None:
    peak = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)  # 15:00 UTC+8, a Wednesday
    assert peak_factor(policy(), peak) == 1.0
    assert peak_factor(policy(), peak + timedelta(hours=4)) == 0.5
    saturday = datetime(2026, 10, 10, 7, 0, tzinfo=UTC)
    assert peak_factor(policy(), saturday) == 0.5
    assert peak_factor(policy(peak=None), saturday) == 1.0


def test_a_recorded_credit_figure_is_used_as_given() -> None:
    assert credits_for_usage(policy(), spend(42.0)) == 42.0


def test_an_unrated_model_is_charged_at_the_dearest_rate_and_named() -> None:
    rec = ModelGlmUsageRecord(
        run_id="a",
        started_at=NOW,
        model="m-new",
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=10_000,
        credits=None,
    )
    assert credits_for_usage(policy(), rec) == pytest.approx(10_000 * 30 / 10_000 * 0.5)
    result = decide(usage=(rec,))
    assert result.unrated_models == ("m-new",)


def test_the_week_starts_on_the_anchor_and_rolls_every_seven_days() -> None:
    assert week_start(policy(), NOW) == ANCHOR + timedelta(days=7)
    assert (
        week_start(policy(), ANCHOR + timedelta(days=7) - timedelta(seconds=1))
        == ANCHOR
    )


# --- what is left ---------------------------------------------------------------------------------------


def test_nothing_spent_leaves_the_whole_window_and_week() -> None:
    result = decide()
    assert (result.window.cap, result.window.used, result.window.remaining) == (
        1000.0,
        0.0,
        1000.0,
    )
    assert result.window.reserve == 100.0
    assert result.window.usable == 900.0
    assert result.window.resets_at is None
    assert (result.week.cap, result.week.used) == (5000.0, 0.0)
    assert result.week.resets_at == ANCHOR + timedelta(days=14)


def test_the_window_counts_only_the_last_window_hours() -> None:
    result = decide(
        usage=(
            spend(300, ago=timedelta(hours=1)),
            spend(200, ago=timedelta(hours=4, minutes=30)),
            spend(900, ago=timedelta(hours=5, minutes=1)),
        )
    )
    assert result.window.used == 500.0
    assert result.window.remaining == 500.0
    # The 4h30 spend leaves the window at 5h after it was made.
    assert result.window.resets_at == NOW - timedelta(hours=4, minutes=30) + timedelta(
        hours=5
    )
    # All three are in this week.
    assert result.week.used == 1400.0


def test_the_week_counts_from_the_anchor_period_not_the_last_seven_days() -> None:
    last_week = spend(
        4000, ago=timedelta(days=4)
    )  # 2026-10-03, before this week's start 2026-10-05
    result = decide(usage=(last_week, spend(100)))
    assert result.week.used == 100.0


# --- the verdicts ---------------------------------------------------------------------------------------


def test_unused_allowance_prefers_glm_ahead_of_the_costlier_rungs_for_an_approved_class() -> (
    None
):
    chain = (
        ModelGlmChainRung(name="local", is_glm=False, cost_rank=0),
        ModelGlmChainRung(name="codex", is_glm=False, cost_rank=2),
        ModelGlmChainRung(name="glm", is_glm=True, cost_rank=1),
        ModelGlmChainRung(name="sonnet", is_glm=False, cost_rank=4),
    )
    result = decide(chain=chain)
    assert result.verdict is EnumGlmAllowanceVerdict.PREFER_GLM
    assert result.ordered_rungs == ("local", "glm", "codex", "sonnet")
    assert result.skipped == ()


def test_glm_never_moves_ahead_of_a_cheaper_rung() -> None:
    result = decide()
    assert result.verdict is EnumGlmAllowanceVerdict.PREFER_GLM
    assert result.ordered_rungs == ("local", "glm_flash", "glm", "haiku", "sonnet")


def test_with_half_the_window_gone_glm_stays_where_the_chain_put_it() -> None:
    chain = (
        ModelGlmChainRung(name="codex", is_glm=False, cost_rank=2),
        ModelGlmChainRung(name="glm", is_glm=True, cost_rank=1),
    )
    result = decide(chain=chain, usage=(spend(600),))
    assert result.verdict is EnumGlmAllowanceVerdict.ALLOW_GLM
    assert result.ordered_rungs == ("codex", "glm")


def test_the_week_can_hold_back_the_preference_while_the_window_is_empty() -> None:
    chain = (
        ModelGlmChainRung(name="codex", is_glm=False, cost_rank=2),
        ModelGlmChainRung(name="glm", is_glm=True, cost_rank=1),
    )
    result = decide(chain=chain, usage=(spend(3000, ago=timedelta(days=1)),))
    assert result.verdict is EnumGlmAllowanceVerdict.ALLOW_GLM
    assert result.ordered_rungs == ("codex", "glm")


def test_near_the_window_cap_every_glm_rung_is_dropped_and_work_goes_to_the_next_rung() -> (
    None
):
    # usable = 900 - 880 = 20 = the call reserve; one more call would cross it, so 21 used more.
    result = decide(usage=(spend(881),))
    assert result.verdict is EnumGlmAllowanceVerdict.SKIP_GLM
    assert result.ordered_rungs == ("local", "haiku", "sonnet")
    assert [(s.rung, s.reason) for s in result.skipped] == [
        ("glm_flash", EnumGlmSkipReason.WINDOW_NEAR_CAP),
        ("glm", EnumGlmSkipReason.WINDOW_NEAR_CAP),
    ]


def test_the_estimate_of_the_call_decides_what_near_the_cap_means() -> None:
    used = (spend(700),)  # usable 200
    assert (
        decide(usage=used, estimated_credits=200.0).verdict
        is not EnumGlmAllowanceVerdict.SKIP_GLM
    )
    assert (
        decide(usage=used, estimated_credits=200.5).verdict
        is EnumGlmAllowanceVerdict.SKIP_GLM
    )


def test_an_exhausted_window_drops_glm_and_so_does_an_overdrawn_one() -> None:
    for spent in (1000, 1500):
        result = decide(usage=(spend(spent),))
        assert result.verdict is EnumGlmAllowanceVerdict.SKIP_GLM
        assert result.window.remaining == max(0.0, 1000 - spent)
        assert result.window.usable == 0.0


def test_near_the_weekly_cap_glm_is_dropped_with_the_week_as_the_reason() -> None:
    result = decide(usage=(spend(4490, ago=timedelta(days=1)),))
    assert result.verdict is EnumGlmAllowanceVerdict.SKIP_GLM
    assert {s.reason for s in result.skipped} == {EnumGlmSkipReason.WEEK_NEAR_CAP}


def test_a_new_week_gives_the_allowance_back() -> None:
    later = NOW + timedelta(days=7)
    result = decide(now=later, usage=(spend(4990, ago=timedelta(days=8)),))
    assert result.verdict is EnumGlmAllowanceVerdict.PREFER_GLM


def test_a_class_the_evals_did_not_approve_never_gets_a_glm_rung() -> None:
    result = decide(task_class="code_generation")
    assert result.verdict is EnumGlmAllowanceVerdict.SKIP_GLM
    assert result.ordered_rungs == ("local", "haiku", "sonnet")
    assert {s.reason for s in result.skipped} == {EnumGlmSkipReason.CLASS_NOT_APPROVED}


@pytest.mark.parametrize(
    ("budget", "allowed"), [(None, False), (599, False), (600, True), (900, True)]
)
def test_code_review_runs_on_glm_only_at_the_approved_budget(
    budget: int | None, allowed: bool
) -> None:
    result = decide(task_class="code_review", budget_s=budget)
    assert (result.verdict is not EnumGlmAllowanceVerdict.SKIP_GLM) is allowed
    if not allowed:
        assert {s.reason for s in result.skipped} == {
            EnumGlmSkipReason.BUDGET_BELOW_CLASS_MINIMUM
        }


def test_a_class_approved_at_any_budget_runs_with_an_unknown_budget() -> None:
    assert (
        decide(task_class="document", budget_s=None).verdict
        is EnumGlmAllowanceVerdict.PREFER_GLM
    )


def test_a_chain_with_no_glm_rung_is_left_as_it_is() -> None:
    chain = (
        ModelGlmChainRung(name="local", is_glm=False, cost_rank=0),
        ModelGlmChainRung(name="sonnet", is_glm=False, cost_rank=4),
    )
    result = decide(chain=chain, usage=(spend(1000),))
    assert result.verdict is EnumGlmAllowanceVerdict.NO_GLM_RUNG
    assert result.ordered_rungs == ("local", "sonnet")
    assert result.skipped == ()


# --- provider refusals ----------------------------------------------------------------------------------


def test_a_rate_refusal_holds_glm_back_for_the_cooldown_only() -> None:
    refusal = ModelGlmRefusalRecord(
        observed_at=NOW - timedelta(seconds=60), scope=EnumGlmRefusalScope.RATE
    )
    during = decide(refusals=(refusal,))
    assert during.verdict is EnumGlmAllowanceVerdict.SKIP_GLM
    assert {s.reason for s in during.skipped} == {
        EnumGlmSkipReason.PROVIDER_REFUSAL_COOLDOWN
    }
    assert during.blocked_until == refusal.observed_at + timedelta(seconds=120)
    after = decide(now=NOW + timedelta(seconds=61), refusals=(refusal,))
    assert after.verdict is EnumGlmAllowanceVerdict.PREFER_GLM
    assert after.blocked_until is None


def test_a_window_refusal_holds_glm_until_the_window_has_room_again() -> None:
    refusal = ModelGlmRefusalRecord(
        observed_at=NOW - timedelta(hours=1), scope=EnumGlmRefusalScope.WINDOW
    )
    result = decide(refusals=(refusal,))
    assert result.verdict is EnumGlmAllowanceVerdict.SKIP_GLM
    assert result.blocked_until == refusal.observed_at + timedelta(hours=5)
    later = decide(now=NOW + timedelta(hours=4, minutes=1), refusals=(refusal,))
    assert later.verdict is EnumGlmAllowanceVerdict.PREFER_GLM


def test_a_week_refusal_holds_glm_until_the_weekly_reset() -> None:
    refusal = ModelGlmRefusalRecord(
        observed_at=NOW - timedelta(hours=1), scope=EnumGlmRefusalScope.WEEK
    )
    result = decide(refusals=(refusal,))
    assert result.verdict is EnumGlmAllowanceVerdict.SKIP_GLM
    assert result.blocked_until == ANCHOR + timedelta(days=14)


def test_the_latest_blocking_refusal_wins() -> None:
    old_week = ModelGlmRefusalRecord(
        observed_at=NOW - timedelta(days=6), scope=EnumGlmRefusalScope.WEEK
    )
    new_rate = ModelGlmRefusalRecord(
        observed_at=NOW - timedelta(seconds=10), scope=EnumGlmRefusalScope.RATE
    )
    result = decide(refusals=(old_week, new_rate))
    assert result.blocked_until == new_rate.observed_at + timedelta(seconds=120)


# --- boundaries -----------------------------------------------------------------------------------------

_CODEX_THEN_GLM = (
    ModelGlmChainRung(name="codex", is_glm=False, cost_rank=2),
    ModelGlmChainRung(name="glm", is_glm=True, cost_rank=1),
)


def test_a_call_that_exactly_fits_the_usable_week_is_allowed() -> None:
    # usable week = 5000 - 500 reserve - 4480 = 20 = the call reserve.
    result = decide(usage=(spend(4480, ago=timedelta(days=1)),))
    assert result.week.usable == 20.0
    assert result.verdict is not EnumGlmAllowanceVerdict.SKIP_GLM


def test_exactly_the_preferred_fraction_left_still_prefers_glm() -> None:
    result = decide(chain=_CODEX_THEN_GLM, usage=(spend(500),))
    assert result.window.remaining_fraction == 0.5
    assert result.verdict is EnumGlmAllowanceVerdict.PREFER_GLM
    assert result.ordered_rungs == ("glm", "codex")


def test_a_refusal_whose_cooldown_ends_now_no_longer_holds_glm_back() -> None:
    refusal = ModelGlmRefusalRecord(
        observed_at=NOW - timedelta(seconds=120), scope=EnumGlmRefusalScope.RATE
    )
    result = decide(refusals=(refusal,))
    assert result.blocked_until is None
    assert result.verdict is EnumGlmAllowanceVerdict.PREFER_GLM


def test_glm_does_not_pass_a_rung_of_the_same_cost_rank() -> None:
    chain = (
        ModelGlmChainRung(name="codex", is_glm=False, cost_rank=1),
        ModelGlmChainRung(name="glm", is_glm=True, cost_rank=1),
    )
    assert decide(chain=chain).ordered_rungs == ("codex", "glm")


def test_spend_exactly_one_window_old_has_left_the_window_and_one_at_the_week_start_counts() -> (
    None
):
    result = decide(usage=(spend(300, ago=timedelta(hours=5)),))
    assert result.window.used == 0.0
    start = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
    at_start = spend(300, ago=NOW - start)
    assert decide(usage=(at_start,)).week.used == 300.0
    assert (
        decide(usage=(spend(300, ago=NOW - start + timedelta(seconds=1)),)).week.used
        == 0.0
    )


# --- shape ----------------------------------------------------------------------------------------------


def test_the_decision_is_deterministic_and_does_not_mutate_the_request() -> None:
    req = request(usage=(spend(100), spend(50, ago=timedelta(hours=2))))
    before = req.model_dump_json()
    first = HandlerGlmAllowance().handle(req)
    assert HandlerGlmAllowance().handle(req) == first
    assert req.model_dump_json() == before


def test_a_policy_missing_any_deployment_fact_is_refused() -> None:
    full = policy().model_dump(mode="json")
    for key in list(full):
        partial = {k: v for k, v in full.items() if k != key}
        with pytest.raises(ValidationError):
            ModelGlmAllowancePolicy.model_validate(partial)


def test_the_policy_refuses_a_duplicate_declaration_and_an_unknown_key() -> None:
    with pytest.raises(ValidationError):
        policy(approved_classes=[{"task_class": "x", "min_budget_s": None}] * 2)
    with pytest.raises(ValidationError):
        policy(surprise=1)


def test_handler_has_the_canonical_signature() -> None:
    sig = inspect.signature(HandlerGlmAllowance.handle)
    assert list(sig.parameters) == ["self", "request"]
    hints = inspect.get_annotations(HandlerGlmAllowance.handle, eval_str=True)
    assert hints["request"] is ModelGlmAllowanceRequest
    assert hints["return"] is ModelGlmAllowanceResult
    assert issubclass(NodeGlmAllowanceCompute, HandlerGlmAllowance)


def test_the_contract_declares_its_topics_and_the_handler_does_not_hardcode_them() -> (
    None
):
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    dispatch = contract["runtime_dispatch"]
    assert (
        dispatch["command_topic"]
        == "onex.cmd.omnimarket.glm-allowance-evaluate-requested.v1"
    )
    assert (
        dispatch["terminal_events"]["success"]
        == "onex.evt.omnimarket.glm-allowance-evaluated.v1"
    )
    assert contract["handler"]["class"] == "HandlerGlmAllowance"
    source = (NODE / "handlers/handler_glm_allowance.py").read_text()
    assert "onex.evt" not in source
    assert "onex.cmd" not in source


def test_no_packaged_config_carries_the_allowance() -> None:
    configs = Path(__file__).resolve().parents[4] / "src/omnimarket/configs"
    for path in configs.glob("*.yaml"):
        text = path.read_text()
        for key in ("window_credits", "weekly_credits", "week_anchor", "cap_codes"):
            assert key not in text, f"{path.name} carries the deployment fact {key}"
