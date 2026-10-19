# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Role C scoring checks, including withheld labels and failed observations."""

from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_context_selection_policy_compute import (
    HandlerContextSelectionEvaluation,
)
from omnimarket.nodes.node_context_selection_policy_compute.models.model_selection_evaluation import (
    ModelSelectionEvaluationRequest,
    ModelSelectionEvaluationResult,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter


def payload() -> dict:
    items = [
        {
            "candidate": {
                "factor": "lane_rules",
                "source": "required",
                "is_required": True,
            },
            "token_estimate": 4,
        },
        {
            "candidate": {
                "factor": "exemplar",
                "source": "relevant",
                "is_required": False,
                "effectiveness_score": 0.9,
            },
            "token_estimate": 7,
        },
        {
            "candidate": {
                "factor": "exemplar",
                "source": "other",
                "is_required": False,
                "effectiveness_score": 0.5,
            },
            "token_estimate": 3,
        },
    ]
    cases = [
        {
            "case_id": "normal",
            "profile_id": "compact",
            "manifest_hash": "a" * 64,
            "items": items,
            "relevant_optional_sources": ["relevant"],
            "resolved_required_factors": ["lane_rules"],
            "token_budget": 20,
            "max_optional": 1,
        },
        {
            "case_id": "withheld",
            "profile_id": "compact",
            "manifest_hash": "b" * 64,
            "items": copy.deepcopy([items[0], items[2]]),
            "relevant_optional_sources": ["relevant"],
            "withheld_relevant_sources": ["relevant"],
            "resolved_required_factors": ["lane_rules"],
            "token_budget": 20,
            "max_optional": 1,
        },
    ]
    observations = [
        {
            "profile_id": "compact",
            "case_id": case,
            "rung": "selector",
            "repeat": repeat,
            "selected_sources": ["required", selected],
            "prompt_tokens": 10,
            "completion_tokens": 2,
        }
        for case, selected in [("normal", "relevant"), ("withheld", "other")]
        for repeat in (1, 2, 3)
    ]
    return {
        "profile_ids": ["compact"],
        "rungs": ["selector"],
        "cases": cases,
        "observations": observations,
    }


def evaluate(raw: dict):
    return HandlerContextSelectionEvaluation().handle(
        ModelSelectionEvaluationRequest.model_validate(raw)
    )


def test_recall_tokens_and_control_are_reported_for_every_repeat():
    result = evaluate(payload())
    assert result.valid
    assert len(result.rows) == 12
    normal = [r for r in result.rows if r.case_id == "normal"]
    control = [r for r in result.rows if r.case_id == "withheld"]
    assert all(r.recall == 1.0 and r.package_tokens == 11 for r in normal)
    assert all(r.recall == 0.0 and r.missed_sources == ("relevant",) for r in control)
    assert all(r.package_tokens == 7 for r in control)
    assert result.recommendations[0].eligible_rungs == ("selector",)
    assert result.alternatives_status == "not_evaluated"
    assert result.rows == evaluate(payload()).rows


def test_one_repeat_below_baseline_prevents_recommendation():
    raw = payload()
    raw["observations"][1]["selected_sources"] = ["required", "other"]
    result = evaluate(raw)
    assert result.valid
    assert result.recommendations[0].eligible_rungs == ()
    summary = next(
        s for s in result.summaries if s.rung == "selector" and s.repeat == 2
    )
    assert summary.recall == 0.0
    assert summary.relevant_count == 2


def test_parse_failure_counts_as_miss_without_claiming_a_package():
    raw = payload()
    raw["observations"][0].update(status="parse_failure", selected_sources=[])
    result = evaluate(raw)
    assert result.valid
    row = next(
        r
        for r in result.rows
        if r.rung == "selector" and r.case_id == "normal" and r.repeat == 1
    )
    assert row.recall == 0.0
    assert row.package_tokens is None
    summary = next(
        s for s in result.summaries if s.rung == "selector" and s.repeat == 1
    )
    assert summary.parse_failures == 1
    assert summary.relevant_count == 2
    assert summary.prompt_tokens == 20
    assert summary.completion_tokens == 4
    assert result.recommendations[0].eligible_rungs == ()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_repeat",
        "missing_rung",
        "missing_profile",
        "duplicate",
        "unknown_case",
        "no_control",
    ],
)
def test_incomplete_or_ambiguous_matrices_fail_closed(mutation):
    raw = payload()
    if mutation == "missing_repeat":
        raw["observations"].pop()
    elif mutation == "missing_rung":
        raw["rungs"].append("unrecorded")
    elif mutation == "missing_profile":
        raw["profile_ids"].append("unrecorded")
    elif mutation == "duplicate":
        raw["observations"].append(copy.deepcopy(raw["observations"][0]))
    elif mutation == "unknown_case":
        raw["observations"][0]["case_id"] = "unknown"
    elif mutation == "no_control":
        raw["cases"][1]["withheld_relevant_sources"] = []
    result = evaluate(raw)
    assert not result.valid
    assert result.errors
    assert not result.recommendations


@pytest.mark.parametrize(
    "sources",
    [
        ["relevant"],
        ["required", "unknown"],
        ["required", "required"],
        ["required", "relevant", "other"],
    ],
)
def test_invalid_packages_are_not_recorded_or_recommended(sources):
    raw = payload()
    raw["observations"][0]["selected_sources"] = sources
    result = evaluate(raw)
    assert not result.valid
    assert not result.rows
    assert not result.recommendations


def test_budget_overflow_is_rejected():
    raw = payload()
    raw["cases"][0]["token_budget"] = 5
    assert not evaluate(raw).valid


def test_baseline_uses_score_then_source_and_preserves_required():
    raw = payload()
    items = raw["cases"][0]["items"]
    items[2]["candidate"].update(source="aaa", effectiveness_score=0.9)
    result = evaluate(raw)
    assert result.valid
    row = next(r for r in result.rows if r.rung == "baseline" and r.case_id == "normal")
    assert row.selected_sources == ("required", "aaa")
    assert row.recall == 0.0
    assert row.package_tokens == 7


def test_baseline_skips_item_that_does_not_fit_and_keeps_labels_in_denominator():
    raw = payload()
    raw["cases"][0]["token_budget"] = 8
    raw["observations"][0]["selected_sources"] = ["required", "other"]
    raw["observations"][1]["selected_sources"] = ["required", "other"]
    raw["observations"][2]["selected_sources"] = ["required", "other"]
    result = evaluate(raw)
    assert result.valid
    baseline = next(
        r for r in result.rows if r.rung == "baseline" and r.case_id == "normal"
    )
    assert baseline.selected_sources == ("required", "other")
    assert baseline.recall == 0.0


def test_control_must_withhold_a_labelled_optional_item():
    raw = payload()
    raw["cases"][1]["withheld_relevant_sources"] = ["other"]
    assert not evaluate(raw).valid


def test_labels_may_not_name_required_items():
    raw = payload()
    raw["cases"][0]["relevant_optional_sources"] = ["required"]
    assert not evaluate(raw).valid


def test_required_sources_resolved_by_profile_must_exist_in_candidates():
    raw = payload()
    raw["cases"][0]["resolved_required_factors"].append("invariants")
    assert not evaluate(raw).valid


def test_input_rejects_invalid_repeat_and_unknown_fields():
    raw = payload()
    raw["observations"][0]["repeat"] = 4
    with pytest.raises(ValidationError):
        ModelSelectionEvaluationRequest.model_validate(raw)
    raw = payload()
    raw["alternatives"] = ["unmeasured"]
    with pytest.raises(ValidationError):
        ModelSelectionEvaluationRequest.model_validate(raw)


def test_contract_routes_typed_evaluation_and_declares_three_repeats():
    contract = yaml.safe_load(
        Path(
            "src/omnimarket/nodes/node_context_selection_policy_compute/contract.yaml"
        ).read_text()
    )
    route = next(
        r
        for r in contract["handler_routing"]["handlers"]
        if r["operation"] == "evaluate_context_selection"
    )
    assert route["handler"]["name"] == "HandlerContextSelectionEvaluation"
    assert route["event_model"]["name"] == "ModelSelectionEvaluationRequest"
    assert contract["evaluation_policy"]["repeats"] == 3


@pytest.mark.asyncio
async def test_contract_handler_and_model_execute_over_runtime_bus(
    integration_event_bus,
):
    from omnibase_infra.runtime.auto_wiring.models.model_handler_routing import (
        ModelHandlerRouting,
    )

    contract = yaml.safe_load(
        Path(
            "src/omnimarket/nodes/node_context_selection_policy_compute/contract.yaml"
        ).read_text()
    )
    routing = ModelHandlerRouting.model_validate(contract["handler_routing"])
    route = next(
        r for r in routing.handlers if r.operation == "evaluate_context_selection"
    )
    handler_type = getattr(
        importlib.import_module(route.handler.module), route.handler.name
    )
    assert handler_type is HandlerContextSelectionEvaluation
    request_type = getattr(
        importlib.import_module(route.event_model.module), route.event_model.name
    )
    adapter = LocalRuntimeBusAdapter(
        handler=handler_type(),
        handler_name=route.handler.name,
        input_model_cls=request_type,
        output_topic=contract["terminal_event"],
        bus=integration_event_bus,
    )
    await integration_event_bus.start()
    await integration_event_bus.subscribe(
        route.topic, on_message=adapter.on_message, group_id="selection-evaluation-test"
    )
    await integration_event_bus.publish(
        route.topic,
        key=None,
        value=request_type.model_validate(payload()).model_dump_json().encode(),
    )
    history = await integration_event_bus.get_event_history(
        topic=contract["terminal_event"]
    )
    await integration_event_bus.close()
    assert len(history) == 1
    result = ModelSelectionEvaluationResult.model_validate(json.loads(history[0].value))
    assert result.valid
    assert len(result.rows) == 12
    assert result.recommendations[0].eligible_rungs == ("selector",)


def test_profile_scores_are_independent_and_all_profiles_need_controls():
    raw = payload()
    second = copy.deepcopy(raw)
    raw["profile_ids"].append("per_prompt")
    for case in second["cases"]:
        case["profile_id"] = "per_prompt"
    for observation in second["observations"]:
        observation["profile_id"] = "per_prompt"
    second["observations"][0]["selected_sources"] = ["required", "other"]
    raw["cases"].extend(second["cases"])
    raw["observations"].extend(second["observations"])
    result = evaluate(raw)
    assert result.valid
    by_profile = {r.profile_id: r.eligible_rungs for r in result.recommendations}
    assert by_profile == {"compact": ("selector",), "per_prompt": ()}
    raw["cases"][-1]["withheld_relevant_sources"] = []
    assert not evaluate(raw).valid


def test_unavailable_rung_is_not_eligible_even_when_baseline_misses_every_label():
    raw = payload()
    raw["cases"][0]["max_optional"] = 0
    for observation in raw["observations"]:
        if observation["case_id"] == "normal":
            observation.update(status="unavailable", selected_sources=[])
    result = evaluate(raw)
    assert result.valid
    assert result.recommendations[0].eligible_rungs == ()
    summary = next(
        s for s in result.summaries if s.rung == "selector" and s.repeat == 1
    )
    assert summary.unavailable == 1


def test_recorded_results_do_not_depend_on_candidate_enumeration():
    raw = payload()
    before = evaluate(raw)
    for case in raw["cases"]:
        case["items"].reverse()
    assert before == evaluate(raw)
