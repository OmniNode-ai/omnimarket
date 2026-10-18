# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21142: TE.5 hard gates, Role B measurements, and contract-to-bus chains."""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from math import fsum
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

import omnimarket.nodes.node_context_role_adoption_verdict_compute as node_package
from omnimarket.nodes.node_context_role_adoption_verdict_compute.handlers.handler_role_adoption_verdict import (
    HandlerRoleAdoptionVerdict,
    _cost,
)
from omnimarket.nodes.node_context_role_adoption_verdict_compute.models.model_role_adoption_verdict import (
    EnumAdoptionOutcome,
    EnumGateMetric,
    ModelLatency,
    ModelRoleAdoptionVerdictRequest,
    ModelRoleAdoptionVerdictResult,
    ModelRoutingPromotion,
    ModelRungEvaluation,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.context-role-adoption-verdict-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.context-role-adoption-verdict-completed.v1"
HANDLER = HandlerRoleAdoptionVerdict()


def _run_case(case_id: str, repeat: int = 1, **overrides: Any) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "repeat": repeat,
        "recalled_item_ids": ["p1", "p2"] if case_id == "c1" else ["p3"],
        "next_action_correct": True,
        **overrides,
    }


def _complete_runs(**overrides: Any) -> list[dict[str, Any]]:
    return [
        _run_case(case_id, repeat, **overrides)
        for case_id in ("c1", "c2")
        for repeat in range(1, 4)
    ]


def _rung(rung_id: str = "r1", **overrides: Any) -> dict[str, Any]:
    return {
        "rung_id": rung_id,
        "served_model_id": f"served-{rung_id}",
        "mode": "whole",
        "runs": _complete_runs(),
        **overrides,
    }


def _request(*rungs: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Two-case round-one fixture, with one reference repeat per case."""
    return {
        "evaluation_id": "round-one",
        "task_class": "extraction_proposer",
        "repeats_required": 3,
        "cases": [
            {
                "case_id": "c1",
                "confirmed_lost_ruling": True,
                "labelled_item_ids": ["p1", "p2"],
                "negative_control_ids": ["n1"],
                "already_done_item_ids": ["d1"],
            },
            {
                "case_id": "c2",
                "confirmed_lost_ruling": False,
                "labelled_item_ids": ["p3"],
            },
        ],
        "baseline": _rung(
            "baseline",
            runs=[
                _run_case("c1", recalled_item_ids=["p1"], next_action_correct=False),
                _run_case("c2"),
            ],
        ),
        "control_without_events": _rung(
            "control",
            runs=[_run_case("c1", next_action_correct=False), _run_case("c2")],
        ),
        "rungs": list(rungs),
        "cascade_min_tokens_in": 0,
        "cascade_min_cost_usd": 0.0,
        **overrides,
    }


def _decide(payload: dict[str, Any]) -> ModelRoleAdoptionVerdictResult:
    return HANDLER.handle(ModelRoleAdoptionVerdictRequest.model_validate(payload))


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["node_type"] == "compute"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert contract["externally_consumed_topics"] == [TERMINAL_TOPIC]
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["descriptor"]["idempotent"] is True
    assert (
        contract["handler_routing"]["handlers"][0]["operation"]
        == "decide_role_adoption"
    )
    assert contract["metadata"]["related_tickets"] == ["OMN-21142"]
    assert contract["metadata"]["created"] == "2026-10-18"
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert handler_type is HandlerRoleAdoptionVerdict
    assert issubclass(node_package.NodeContextRoleAdoptionVerdictCompute, handler_type)
    for side, model in (
        ("input_model", ModelRoleAdoptionVerdictRequest),
        ("output_model", ModelRoleAdoptionVerdictResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
        assert binding[side] == f"{model.__module__}.{model.__name__}"
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert (
        registered["node_context_role_adoption_verdict_compute"].load() is node_package
    )
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    canonical = yaml.safe_load(
        (NODE_DIR.parent / "node_ci_state_verdict_compute/metadata.yaml").read_text()
    )
    assert metadata["omnibase_core_compat"] == canonical["omnibase_core_compat"]
    assert metadata["pack"] == "core"
    assert metadata["capabilities"]["side_effect_class"] == "pure"
    assert metadata["tags"] == ["compute", "context", "evaluation"]
    assert metadata["dependencies"] == ["pydantic>=2.11.0"]
    assert metadata["display_name"] == "Context Role Adoption Verdict Compute"


def test_complete_rung_passes_and_promotes_only_a_routing_contract_entry() -> None:
    result = _decide(_request(_rung()))
    assert result.evaluation_id == "round-one"
    assert result.task_class == "extraction_proposer"
    assert result.outcome is EnumAdoptionOutcome.ADOPT
    assert not result.failed
    assert result.passed[0].recall == 1.0
    assert result.lowest_cost == ("r1:whole",)
    assert result.promotions == (
        ModelRoutingPromotion(
            task_class="extraction_proposer",
            rung_id="r1",
            served_model_id="served-r1",
            mode="whole",
        ),
    )
    assert result.promotions[0].change == "routing_contract_entry"
    assert result.promotions[0].separate_pr is True
    assert result.recommendation == (
        "promote r1:whole for task class extraction_proposer by adding a routing-contract "
        "entry in a separate PR; no profile is edited"
    )


def test_every_failing_rung_names_its_metrics_and_cases() -> None:
    ruling = _rung("ruling")
    ruling["runs"][1]["recalled_item_ids"] = ["p1"]
    negative = _rung("negative")
    negative["runs"][0]["recalled_item_ids"].append("n1")
    duplicate = _rung("duplicate")
    duplicate["runs"][0]["proposed_item_ids"] = ["d1"]
    coverage = _rung("coverage", runs=_complete_runs()[:-1])
    action = _rung("action", runs=_complete_runs(next_action_correct=False))
    rungs = [ruling, negative, duplicate, coverage, action, _rung("passing")]
    result = _decide(_request(*rungs))
    assert {r.rung_id for r in (*result.passed, *result.failed)} == {
        r["rung_id"] for r in rungs
    }
    failures = {r.rung_id: r.failures for r in result.failed}
    assert {
        name: {(f.metric, f.case_id, f.repeat, f.detail) for f in entries}
        for name, entries in failures.items()
    } == {
        "ruling": {(EnumGateMetric.RULING_RECALL, "c1", 2, "missing p2")},
        "negative": {(EnumGateMetric.NEGATIVE_CONTROL, "c1", 1, "credited n1")},
        "duplicate": {
            (EnumGateMetric.DUPLICATE_WORK, "c1", 1, "proposes already-done d1")
        },
        "coverage": {(EnumGateMetric.COVERAGE, "c2", 3, "no recorded run")},
        "action": {
            (
                EnumGateMetric.NEXT_ACTION_BELOW_CONTROL,
                "c2",
                None,
                "rung 0 below control 1",
            ),
            (
                EnumGateMetric.NEXT_ACTION_BELOW_BASELINE,
                "c2",
                None,
                "rung 0 below baseline 1",
            ),
        },
    }
    assert all(f.case_id for r in result.failed for f in r.failures)
    assert [r.rung_id for r in result.failed] == sorted(failures)


def test_recall_below_baseline_names_the_non_confirmed_case() -> None:
    rung = _rung()
    for run in rung["runs"]:
        if run["case_id"] == "c2":
            run["recalled_item_ids"] = []
    payload = _request(rung)
    # Baseline has full recall, making c2's loss an aggregate shortfall.
    payload["baseline"]["runs"][0]["recalled_item_ids"] = ["p1", "p2"]
    result = _decide(payload)
    (failure,) = result.failed[0].failures
    assert (failure.metric, failure.case_id, failure.repeat, failure.detail) == (
        EnumGateMetric.RECALL_BELOW_BASELINE,
        "c2",
        None,
        "rung 0 below baseline 1",
    )


def test_equal_aggregate_recall_and_action_do_not_gate_a_per_case_shortfall() -> None:
    rung = _rung()
    for run in rung["runs"]:
        run["next_action_correct"] = run["case_id"] == "c1"
        if run["case_id"] == "c2":
            run["recalled_item_ids"] = []
    payload = _request(rung)
    # Different reference repeat counts must not change the case weights.
    payload["baseline"]["runs"] += [_run_case("c2", 2), _run_case("c2", 3)]
    payload["control_without_events"]["runs"] += [
        _run_case("c2", 2),
        _run_case("c2", 3),
    ]
    result = _decide(payload)
    assert not result.failed
    assert result.passed[0].recall == float(2 / 3)


def test_recall_averages_repeats_exactly_and_reports_fraction_details() -> None:
    rung = _rung()
    for run in rung["runs"]:
        if run["case_id"] == "c1":
            run["recalled_item_ids"] = ["p1"] if run["repeat"] == 1 else []
    payload = _request(rung)
    payload["cases"][0]["confirmed_lost_ruling"] = False
    result = _decide(payload)
    (failure,) = result.failed[0].failures
    assert failure.metric is EnumGateMetric.RECALL_BELOW_BASELINE
    assert failure.case_id == "c1"
    assert failure.detail == "rung 1/6 below baseline 1/2"


def test_failure_order_is_enum_then_case_then_repeat() -> None:
    rung = _rung(runs=list(reversed(_complete_runs(next_action_correct=False))))
    for run in rung["runs"]:
        run["recalled_item_ids"] = ["n1"] if run["case_id"] == "c1" else []
        run["proposed_item_ids"] = ["d1"]
    rung["runs"].pop(0)  # c2 repeat 3
    result = _decide(_request(rung))
    failures = result.failed[0].failures
    order = {metric: index for index, metric in enumerate(EnumGateMetric)}
    keys = [(order[f.metric], f.case_id, f.repeat or 0) for f in failures]
    assert keys == sorted(keys)
    assert {f.metric for f in failures} == set(EnumGateMetric)
    assert [f.detail for f in failures if f.metric is EnumGateMetric.RULING_RECALL] == [
        "missing p1,p2"
    ] * 3


def test_no_rung_passed_preserves_the_deterministic_package_and_disables_cascade() -> (
    None
):
    result = _decide(
        _request(_rung(runs=[]), cascade_min_tokens_in=1, cascade_min_cost_usd=1.0)
    )
    assert result.outcome is EnumAdoptionOutcome.NO_RUNG_PASSED
    assert result.passed == result.lowest_cost == result.promotions == ()
    assert "deterministic package without model events" in result.recommendation
    assert "TA.6 and TA.8 stay unchanged" in result.recommendation
    assert result.cascade_justified is False
    assert result.cascade_reason == (
        "no rung passed the hard gate, so there is no extractor to put a gate in front of"
    )
    assert len(result.failed[0].failures) == 6
    assert all(f.metric is EnumGateMetric.COVERAGE for f in result.failed[0].failures)


@pytest.mark.parametrize(
    ("first_cost", "second_cost", "expected"),
    [
        ((0.1, 20, 10), (0.2, 1, 1), ("a:whole",)),
        ((0.1, 20, 10), (0.1, 20, 9), ("b:whole",)),
        ((0.1, 20, 10), (0.1, 25, 5), ("a:whole", "b:whole")),
    ],
)
def test_lowest_cost_uses_dollars_then_total_tokens_and_keeps_all_ties(
    first_cost: tuple[float, int, int],
    second_cost: tuple[float, int, int],
    expected: tuple[str, ...],
) -> None:
    rungs = [
        _rung(name, runs=_complete_runs(cost_usd=cost, tokens_in=tin, tokens_out=tout))
        for name, (cost, tin, tout) in zip(
            ("b", "a"), (second_cost, first_cost), strict=True
        )
    ]
    result = _decide(_request(*rungs))
    assert result.lowest_cost == expected
    assert tuple(f"{p.rung_id}:{p.mode}" for p in result.promotions) == expected
    assert [r.rung_id for r in result.passed] == ["a", "b"]


def test_modes_are_distinct_candidates_and_keys_are_sorted_as_strings() -> None:
    result = _decide(
        _request(_rung("a", mode="whole"), _rung("a-1"), _rung("a", mode="chunked"))
    )
    assert [(r.rung_id, r.mode) for r in result.passed] == [
        ("a", "chunked"),
        ("a", "whole"),
        ("a-1", "whole"),
    ]
    assert result.lowest_cost == ("a-1:whole", "a:chunked", "a:whole")


@pytest.mark.parametrize(
    ("values", "scored", "matched"),
    [
        ([False] * 6, 6, 0),
        ([True, False, None, True, None, False], 4, 2),
        ([None] * 6, 0, 0),
    ],
)
def test_role_b_tail_summary_is_reported_and_never_gates(
    values: list[bool | None], scored: int, matched: int
) -> None:
    rung = _rung()
    for run, value in zip(rung["runs"], values, strict=True):
        run["tail_summary_matches"] = value
    result = _decide(_request(rung))
    assert result.outcome is EnumAdoptionOutcome.ADOPT
    assert result.failed == ()
    assert result.passed[0].tail_summary_scored == scored
    assert result.passed[0].tail_summary_matched == matched


def test_parse_failures_are_summed_apart_on_passed_and_failed_rungs() -> None:
    result = _decide(
        _request(
            _rung("pass", runs=_complete_runs(parse_failures=2)),
            _rung(
                "fail", runs=_complete_runs(parse_failures=3, next_action_correct=False)
            ),
        )
    )
    assert result.passed[0].parse_failures == 12
    assert result.failed[0].parse_failures == 18
    assert {f.metric for f in result.failed[0].failures} == {
        EnumGateMetric.NEXT_ACTION_BELOW_CONTROL,
        EnumGateMetric.NEXT_ACTION_BELOW_BASELINE,
    }


def test_costs_use_summed_measurements_and_nearest_rank_latency() -> None:
    rung = _rung()
    costs = [1e16, 1.0, 1.0, 0.0, 0.0, 0.0]
    for index, run in enumerate(rung["runs"]):
        run.update(
            tokens_in=index,
            tokens_out=index * 2,
            cost_usd=costs[index],
            latency_ms=60 - index * 10,
        )
    cost = _decide(_request(rung)).passed[0].cost
    assert (cost.tokens_in, cost.tokens_out, cost.cost_usd) == (15, 30, fsum(costs))
    assert cost.latency == ModelLatency(p50_ms=30, p95_ms=60, max_ms=60)
    empty = _cost(ModelRungEvaluation.model_validate(_rung(runs=[])))
    assert empty.latency == ModelLatency(p50_ms=0, p95_ms=0, max_ms=0)
    assert (empty.tokens_in, empty.tokens_out, empty.cost_usd) == (0, 0, 0.0)


@pytest.mark.parametrize(
    ("tokens_threshold", "cost_threshold", "justified"),
    [(60, 0.0, True), (0, 6.0, True), (0, 0.0, False), (61, 6.1, False)],
)
def test_cascade_uses_positive_thresholds_and_measured_cost(
    tokens_threshold: int, cost_threshold: float, justified: bool
) -> None:
    result = _decide(
        _request(
            _rung(runs=_complete_runs(tokens_in=10, cost_usd=1.0)),
            cascade_min_tokens_in=tokens_threshold,
            cascade_min_cost_usd=cost_threshold,
        )
    )
    assert result.cascade_justified is justified
    assert result.cascade_reason == (
        "tokens_in=60, cost_usd=6.0; "
        f"cascade_min_tokens_in={tokens_threshold}, cascade_min_cost_usd={cost_threshold}: "
        f"{'justified' if justified else 'not justified'}"
    )


def test_cascade_uses_the_first_sorted_lowest_cost_key() -> None:
    result = _decide(
        _request(
            _rung("z", runs=_complete_runs(tokens_in=10)),
            _rung("a", runs=_complete_runs(tokens_out=10)),
            cascade_min_tokens_in=60,
        )
    )
    assert result.lowest_cost == ("a:whole", "z:whole")
    assert result.cascade_justified is False
    assert "tokens_in=0," in result.cascade_reason


def test_result_model_has_no_profile_field() -> None:
    assert set(ModelRoleAdoptionVerdictResult.model_fields) == {
        "evaluation_id",
        "task_class",
        "outcome",
        "passed",
        "failed",
        "lowest_cost",
        "promotions",
        "recommendation",
        "cascade_justified",
        "cascade_reason",
    }


@pytest.mark.parametrize(
    ("field", "value"), [("change", "profile_change"), ("separate_pr", False)]
)
def test_promotions_refuse_profile_changes_and_inline_edits(
    field: str, value: Any
) -> None:
    payload = {
        "task_class": "extraction_proposer",
        "rung_id": "r1",
        "served_model_id": "served-r1",
        "mode": "whole",
        field: value,
    }
    with pytest.raises(ValidationError, match=field):
        ModelRoutingPromotion.model_validate(payload)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("unknown_case", "unknown case_id"),
        ("unknown_credit", "outside labels and controls"),
        ("excess_repeat", "exceeds repeats_required"),
        ("missing_baseline_case", "baseline must have a run for every case"),
        (
            "missing_control_case",
            "control_without_events must have a run for every case",
        ),
        ("duplicate_run", r"duplicate \(case_id, repeat\)"),
        ("extra_field", "Extra inputs"),
        ("duplicate_case", "case_id must be unique"),
        ("duplicate_rung", r"duplicate \(rung_id, mode\)"),
        ("duplicate_label", "labelled_item_ids must be unique"),
        ("overlapping_control", "must be disjoint"),
    ],
)
def test_error_chain_refuses_invalid_measurements(mutation: str, message: str) -> None:
    payload = _request(_rung())
    run = payload["rungs"][0]["runs"][0]
    if mutation == "unknown_case":
        run["case_id"] = "unknown"
    elif mutation == "unknown_credit":
        run["recalled_item_ids"] = ["unknown"]
    elif mutation == "excess_repeat":
        run["repeat"] = 4
    elif mutation == "missing_baseline_case":
        payload["baseline"]["runs"].pop()
    elif mutation == "missing_control_case":
        payload["control_without_events"]["runs"].pop()
    elif mutation == "duplicate_run":
        payload["rungs"][0]["runs"].append(run)
    elif mutation == "extra_field":
        payload["profile"] = "changed"
    elif mutation == "duplicate_case":
        payload["cases"].append(payload["cases"][0])
    elif mutation == "duplicate_rung":
        payload["rungs"].append(payload["rungs"][0])
    elif mutation == "duplicate_label":
        payload["cases"][0]["labelled_item_ids"].append("p1")
    elif mutation == "overlapping_control":
        payload["cases"][0]["negative_control_ids"].append("p1")
    with pytest.raises(ValidationError, match=message):
        ModelRoleAdoptionVerdictRequest.model_validate(payload)


@pytest.mark.parametrize("reference", ["baseline", "control_without_events"])
@pytest.mark.parametrize("field", ["case_id", "recalled_item_ids"])
def test_reference_runs_are_validated_too(reference: str, field: str) -> None:
    payload = _request(_rung())
    payload[reference]["runs"][0][field] = (
        "unknown" if field == "case_id" else ["unknown"]
    )
    with pytest.raises(
        ValidationError, match=r"unknown case_id|outside labels and controls"
    ):
        ModelRoleAdoptionVerdictRequest.model_validate(payload)


def test_models_are_frozen_and_nested_extra_fields_are_refused() -> None:
    request = ModelRoleAdoptionVerdictRequest.model_validate(_request(_rung()))
    with pytest.raises(ValidationError, match="frozen"):
        request.rungs[0].runs[0].repeat = 2
    payload = _request(_rung())
    payload["rungs"][0]["runs"][0]["surprise"] = 1
    with pytest.raises(ValidationError, match="Extra inputs"):
        ModelRoleAdoptionVerdictRequest.model_validate(payload)


async def _run(tmp_path: Path, payload: dict[str, Any]) -> RuntimeLocal:
    input_path = tmp_path / "request.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_matches_the_direct_handler(
    tmp_path: Path,
) -> None:
    payload = _request(
        _rung("pass"), _rung("fail", runs=_complete_runs(next_action_correct=False))
    )
    runtime = await _run(tmp_path, payload)
    result = runtime.handler_result
    assert isinstance(result, ModelRoleAdoptionVerdictResult)
    assert result == _decide(payload)
    assert (
        ModelRoleAdoptionVerdictResult.model_validate_json(result.model_dump_json())
        == result
    )


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_result(tmp_path: Path) -> None:
    runtime = await _run(tmp_path, {})
    assert runtime.handler_result is None
