# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic golden and error chains for the TE.4 round-one scorer (OMN-21141)."""

from __future__ import annotations

import importlib
import tomllib
from math import sqrt
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_context_round_one_eval_compute.handlers.handler_round_one_eval import (
    HandlerRoundOneEval,
)
from omnimarket.nodes.node_context_round_one_eval_compute.models import (
    EnumPositiveControl,
    EnumRunMode,
    ModelCaseLabels,
    ModelRoundOneEvalRequest,
    ModelRoundOneEvalResult,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
NODE_NAME = "node_context_round_one_eval_compute"
PACKAGE = f"omnimarket.nodes.{NODE_NAME}"
NODE = ROOT / "src/omnimarket/nodes" / NODE_NAME


def _span(event_id: str = "record", start: int = 10, end: int = 30) -> dict[str, Any]:
    return {"event_id": event_id, "start": start, "end": end}


def _case(case_id: str = "case") -> dict[str, Any]:
    return {
        "case_id": case_id,
        "transcript_tokens": 100,
        "propositions": [
            {"label_id": "p", "kind": "ruling", "span": _span(), "hard_gate": True}
        ],
        "already_done": ["finished"],
    }


def _event(
    event_id: str = "record", start: int = 10, end: int = 30, kind: str = "ruling"
) -> dict[str, Any]:
    return {**_span(event_id, start, end), "kind": kind}


def _run(**overrides: Any) -> dict[str, Any]:
    return {
        "rung_id": "A",
        "served_model_id": "model-a",
        "case_id": "case",
        "repeat": 1,
        "mode": "whole",
        "window_tokens": 50,
        "candidate_id": "candidate",
        "assembly_hash": "assembly",
        "model_outputs_hash": "outputs",
        "final_pack_hash": "pack",
        "body_hash": "body",
        "replayed_model_outputs_hash": "outputs",
        "replayed_final_pack_hash": "pack",
        "replayed_body_hash": "body",
        "admitted": [_event()],
        "answer": "MATCH",
        **overrides,
    }


def _evaluate(
    runs: list[dict[str, Any]],
    cases: list[dict[str, Any]] | None = None,
    baselines: list[dict[str, Any]] | None = None,
    reserve_tokens: int = 0,
) -> ModelRoundOneEvalResult:
    request = ModelRoundOneEvalRequest.model_validate(
        {
            "cases": cases if cases is not None else [_case()],
            "runs": runs,
            "baselines": baselines or [],
            "reserve_tokens": reserve_tokens,
        }
    )
    before = request.model_dump()
    result = HandlerRoundOneEval().handle(request)
    assert request.model_dump() == before
    assert HandlerRoundOneEval().handle(request) == result
    return result


def _rich_case() -> dict[str, Any]:
    case = _case()
    case["propositions"] += [
        {
            "label_id": "old",
            "kind": "decision",
            "span": _span("old"),
            "hard_gate": False,
        },
        {
            "label_id": "fix",
            "kind": "correction",
            "span": _span("fix"),
            "hard_gate": False,
        },
    ]
    case["span_controls"] = [
        {
            "label_id": "partial",
            "control_kind": "partial_quote",
            "controls": "p",
            "span": _span(end=20),
        },
        {
            "label_id": "hypothesis",
            "control_kind": "hypothetical",
            "controls": "p",
            "span": _span("hypothetical"),
        },
    ]
    case["corrected_rulings"] = [
        {"label_id": "corrected", "ruling": "old", "correction": "fix"}
    ]
    case["negative_segments"] = [{"label_id": "negative", "span": _span("negative")}]
    return case


def _perfect_events(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            key: value
            for key, value in {**p["span"], "kind": p["kind"]}.items()
            if key != "same_content_event_ids"
        }
        for p in case["propositions"]
    ]


def test_golden_chain_eligible_rung_and_partial_quote_failure() -> None:
    case = _rich_case()
    # Six runs, each with two calls: sorted latencies 10..120 give p50=60, p95=120.
    runs = [
        _run(
            mode=mode,
            repeat=repeat,
            window_tokens=120,
            admitted=_perfect_events(case),
            calls=[
                {
                    "latency_ms": latency,
                    "tokens_in": 10,
                    "tokens_out": 2,
                    "cost_usd": 0.0001234,
                }
                for latency in (
                    10 * (index * 6 + repeat * 2 - 1),
                    10 * (index * 6 + repeat * 2),
                )
            ],
        )
        for index, mode in enumerate(("whole", "chunked"))
        for repeat in range(1, 4)
    ]
    runs += [
        _run(
            removed_label_ids=["p"],
            admitted=_perfect_events(case)[1:],
            # These values must have no effect on metrics, identity or cost.
            candidate_id="pc-candidate",
            assembly_hash="pc-assembly",
            served_model_id="pc-model",
            parse_failures=7,
            proposed_already_done=["finished"],
            answer="MISMATCH",
            calls=[{"latency_ms": 9999, "tokens_in": 9999, "tokens_out": 9999}],
        ),
        _run(rung_id="B", admitted=[_event(end=20)], answer="MISMATCH"),
    ]
    baseline = {
        "case_id": "case",
        "native_recalled_label_ids": ["p"],
        "native_answer": "MATCH",
        "without_events_answer": "MISMATCH",
    }
    result = _evaluate(runs, [case], [baseline], reserve_tokens=20)
    a, b = result.rungs
    assert result.eligible_rungs == ("A",)
    assert a.runs == 6
    assert (a.recall.successes, a.recall.trials, a.recall.rate) == (18, 18, 1.0)
    assert (a.precision.successes, a.precision.trials) == (18, 18)
    assert a.hard_gate_met
    assert a.recall_not_below_native
    assert a.next_action_met
    assert a.no_duplicate_work
    assert a.passes_metrics_1_to_3
    assert a.positive_control == EnumPositiveControl.MISS_SCORED
    assert a.served_model_ids == ("model-a",)
    assert a.parse_failures == a.negative_segment_admissions == 0
    assert a.zero_metrics == ()
    assert a.per_case[0].runs == 6
    assert not a.per_case[0].whole_chunked_disagree
    assert not a.per_case[0].single_mode_where_both_allowed
    assert [(m.mode.value, m.runs, m.recall.rate) for m in a.modes] == [
        ("chunked", 3, 1.0),
        ("whole", 3, 1.0),
    ]
    assert [
        (c.label_id, c.control_kind, c.runs_scored, c.violations) for c in a.controls
    ] == [
        ("corrected", "corrected_ruling", 6, 0),
        ("hypothesis", "hypothetical", 6, 0),
        ("partial", "partial_quote", 6, 0),
    ]
    assert a.cost is not None
    assert a.cost.model_dump() == {
        "calls": 12,
        "latency_p50_ms": 60,
        "latency_p95_ms": 120,
        "latency_max_ms": 120,
        "tokens_in": 120,
        "tokens_out": 24,
        "cost_usd": 0.001481,
    }
    assert (b.recall.successes, b.recall.trials) == (0, 3)
    assert not b.hard_gate_met
    assert not b.recall_not_below_native
    assert not b.next_action_met
    assert not b.passes_metrics_1_to_3
    assert b.cost is None
    assert b.per_case[0].control_violations == 1
    assert [c.violations for c in b.controls] == [0, 0, 1]
    assert b.zero_metrics == ("next_action", "precision", "recall")
    assert result.native_recall.rate == 0.333333
    assert result.native_next_action.rate == 1.0
    assert result.without_events_next_action.rate == 0.0
    assert result.candidate_id_mismatches == result.assembly_hash_mismatches == ()
    assert result.replay_mismatches == ()
    assert result.unreplayed_runs == 0
    assert result.falsifiers == (
        "B: zero next_action without a positive control",
        "B: zero precision without a positive control",
        "B: zero recall without a positive control",
    )


@pytest.mark.parametrize(
    "event",
    [_event(end=29), _event(kind="decision"), _event(event_id="different")],
    ids=["missing-last-character", "wrong-kind", "wrong-record"],
)
def test_overlap_kind_and_record_do_not_score(event: dict[str, Any]) -> None:
    rung = _evaluate([_run(admitted=[event])]).rungs[0]
    assert rung.recall.successes == rung.precision.successes == 0
    assert not rung.hard_gate_met


def test_same_content_record_and_widened_span_match() -> None:
    case = _case()
    case["propositions"][0]["span"]["same_content_event_ids"] = ["copy"]
    rung = _evaluate([_run(admitted=[_event("copy", 0, 40)])], [case]).rungs[0]
    assert rung.recall.rate == rung.precision.rate == 1.0
    assert rung.hard_gate_met


def test_corrected_ruling_without_correction_is_not_credited() -> None:
    case = _rich_case()
    case["propositions"][1]["hard_gate"] = True
    rung = _evaluate(
        [_run(admitted=[_event(), _event("old", kind="decision")])], [case]
    ).rungs[0]
    assert (rung.recall.successes, rung.recall.trials) == (1, 3)
    assert rung.precision.successes == 2
    assert not rung.hard_gate_met
    assert rung.per_case[0].control_violations == 1
    assert [(c.label_id, c.violations) for c in rung.controls] == [
        ("corrected", 1),
        ("hypothesis", 0),
        ("partial", 0),
    ]


def test_controls_count_runs_once_and_use_their_own_records() -> None:
    case = _rich_case()
    case["span_controls"][1]["span"]["same_content_event_ids"] = ["hyp-copy"]
    result = _evaluate(
        [
            _run(admitted=[_event(), _event("hyp-copy"), _event("hyp-copy")]),
            _run(repeat=2, admitted=[_event("hypothetical", kind="decision")]),
        ],
        [case],
    )
    reports = result.rungs[0].controls
    assert [(c.label_id, c.runs_scored, c.violations) for c in reports] == [
        ("corrected", 2, 0),
        ("hypothesis", 2, 1),
        ("partial", 2, 0),
    ]


def test_negative_admissions_duplicate_work_and_parse_failures() -> None:
    case = _rich_case()
    case["negative_segments"][0]["span"]["same_content_event_ids"] = ["negative-copy"]
    events = [
        *_perfect_events(case),
        _event("negative-copy", 0, 11),
        _event("negative", 30, 40),  # Half-open boundary does not overlap.
    ]
    rung = _evaluate(
        [
            _run(admitted=events, proposed_already_done=["finished"], parse_failures=2),
            _run(repeat=2, admitted=_perfect_events(case), parse_failures=3),
        ],
        [case],
    ).rungs[0]
    assert rung.negative_segment_admissions == 1
    assert rung.parse_failures == 5
    assert (rung.precision.successes, rung.precision.trials) == (6, 8)
    assert rung.duplicate_work_runs == 1
    assert not rung.no_duplicate_work
    assert not rung.passes_metrics_1_to_3
    assert rung.cost is None


@pytest.mark.parametrize("field", ["assembly_hash", "candidate_id"])
def test_identity_mismatch_across_rungs(field: str) -> None:
    result = _evaluate([_run(), _run(rung_id="B", **{field: "different"})])
    assert getattr(result, f"{field}_mismatches") == ("case",)
    other = "candidate_id" if field == "assembly_hash" else "assembly_hash"
    assert getattr(result, f"{other}_mismatches") == ()
    assert result.falsifiers == (f"case: {field} differs between runs",)


@pytest.mark.parametrize(
    "field", ["model_outputs_hash", "final_pack_hash", "body_hash"]
)
def test_replay_mismatch_and_unreplayed_runs_include_positive_controls(
    field: str,
) -> None:
    result = _evaluate(
        [
            _run(**{f"replayed_{field}": "changed"}),
            _run(
                removed_label_ids=["p"],
                admitted=[],
                replayed_model_outputs_hash=None,
                replayed_final_pack_hash=None,
                replayed_body_hash=None,
            ),
            _run(repeat=2, replayed_body_hash=None),
        ]
    )
    mismatch = f"A/case/r1/whole:{field}"
    assert result.replay_mismatches == (mismatch,)
    assert result.unreplayed_runs == 1
    assert result.falsifiers == (
        "1 run(s) carry no replay hashes",
        f"{mismatch} did not reproduce on replay",
    )


def test_replay_mismatch_on_positive_control_is_reported() -> None:
    result = _evaluate(
        [
            _run(),
            _run(removed_label_ids=["p"], admitted=[], replayed_final_pack_hash="bad"),
        ]
    )
    assert result.replay_mismatches == ("A/case/r1/whole:final_pack_hash",)


def test_positive_control_recalling_removed_label_does_not_score_miss() -> None:
    result = _evaluate([_run(), _run(removed_label_ids=["p"])])
    rung = result.rungs[0]
    assert rung.positive_control == EnumPositiveControl.NOT_SCORED
    assert rung.runs == 1
    assert rung.recall.trials == 1
    assert rung.passes_metrics_1_to_3
    assert rung.cost is not None
    assert result.eligible_rungs == ()
    assert result.falsifiers == ("A: positive control did not score the miss",)


def test_every_positive_control_run_must_miss_every_removed_label() -> None:
    case = _rich_case()
    result = _evaluate(
        [
            _run(admitted=_perfect_events(case)),
            _run(removed_label_ids=["p", "fix"], admitted=[]),
            _run(repeat=2, removed_label_ids=["p", "fix"], admitted=[_event()]),
        ],
        [case],
    )
    assert result.rungs[0].positive_control == EnumPositiveControl.NOT_SCORED
    assert result.eligible_rungs == ()


def test_zero_recall_without_positive_control_is_falsified() -> None:
    result = _evaluate([_run(admitted=[])])
    rung = result.rungs[0]
    assert rung.positive_control == EnumPositiveControl.ABSENT
    assert rung.zero_metrics == ("recall",)
    assert result.falsifiers == ("A: zero recall without a positive control",)
    assert (rung.precision.rate, rung.precision.low, rung.precision.high) == (0, 0, 1)


def test_zero_metrics_with_scored_positive_control_have_no_falsifier() -> None:
    result = _evaluate(
        [
            _run(admitted=[_event(end=20)], answer="MISMATCH"),
            _run(removed_label_ids=["p"], admitted=[]),
        ]
    )
    assert result.rungs[0].zero_metrics == ("next_action", "precision", "recall")
    assert result.falsifiers == ()


def test_whole_chunked_disagreement_is_a_finding() -> None:
    result = _evaluate([_run(), _run(mode="chunked", admitted=[])])
    rung = result.rungs[0]
    assert rung.per_case[0].whole_chunked_disagree
    assert rung.per_case[0].modes == (EnumRunMode.CHUNKED, EnumRunMode.WHOLE)
    assert [(m.mode.value, m.hard_gate_met) for m in rung.modes] == [
        ("chunked", False),
        ("whole", True),
    ]
    assert not rung.hard_gate_met
    assert result.falsifiers == ()


@pytest.mark.parametrize(
    ("windows", "expected"), [([120], True), ([119], False), ([120, 119], False)]
)
def test_single_mode_window_includes_reserve_and_uses_minimum(
    windows: list[int], expected: bool
) -> None:
    result = _evaluate(
        [_run(repeat=i, window_tokens=window) for i, window in enumerate(windows, 1)],
        reserve_tokens=20,
    )
    assert result.rungs[0].per_case[0].single_mode_where_both_allowed is expected
    assert result.falsifiers == (
        ("A/case: evaluated in one mode where its window allows both",)
        if expected
        else ()
    )


def test_wilson_interval_matches_formula_and_zero_trials() -> None:
    result = _evaluate([_run(repeat=i, answer="UNAVAILABLE") for i in range(1, 4)])
    interval = result.rungs[0].recall
    z, n, p = 1.959963984540054, 3, 1.0
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    assert (interval.successes, interval.trials) == (3, 3)
    assert (interval.rate, interval.low, interval.high) == (
        1.0,
        round(max(0, centre - half), 6),
        round(min(1, centre + half), 6),
    )
    assert interval.low == 0.438503
    for empty in (result.native_recall, result.rungs[0].next_action):
        assert empty.model_dump() == {
            "successes": 0,
            "trials": 0,
            "rate": 0.0,
            "low": 0.0,
            "high": 1.0,
        }
    assert not result.rungs[0].next_action_met


def test_native_recall_can_fail_a_run_that_meets_its_hard_gate() -> None:
    case = _rich_case()
    baseline = {
        "case_id": "case",
        "native_recalled_label_ids": ["p", "old", "fix"],
        "native_answer": "UNAVAILABLE",
        "without_events_answer": "UNAVAILABLE",
    }
    rung = _evaluate([_run()], [case], [baseline]).rungs[0]
    assert rung.per_case[0].hard_gate_met
    assert not rung.recall_not_below_native
    assert not rung.hard_gate_met
    assert rung.next_action_met
    assert rung.cost is None


@pytest.mark.parametrize(
    ("native", "without_events"),
    [("MATCH", "MISMATCH"), ("MISMATCH", "MATCH")],
)
def test_next_action_must_meet_both_baselines(native: str, without_events: str) -> None:
    baseline = {
        "case_id": "case",
        "native_answer": native,
        "without_events_answer": without_events,
    }
    rung = _evaluate(
        [
            _run(),
            _run(repeat=2, answer="MISMATCH"),
            _run(repeat=3, answer="UNAVAILABLE"),
        ],
        baselines=[baseline],
    ).rungs[0]
    assert (rung.next_action.successes, rung.next_action.trials) == (1, 2)
    assert not rung.next_action_met
    assert rung.cost is None


def test_next_action_rung_baselines_only_include_cases_it_ran() -> None:
    baselines = [
        {
            "case_id": "case",
            "native_answer": "MISMATCH",
            "without_events_answer": "UNAVAILABLE",
        },
        {
            "case_id": "other",
            "native_answer": "MATCH",
            "without_events_answer": "MATCH",
        },
    ]
    result = _evaluate([_run(answer="MISMATCH")], [_case(), _case("other")], baselines)
    assert result.rungs[0].next_action_met
    assert result.native_next_action.rate == 0.5
    assert result.without_events_next_action.rate == 1.0


def test_passing_rung_with_zero_calls_has_zero_costs() -> None:
    rung = _evaluate([_run()]).rungs[0]
    assert rung.cost is not None
    assert all(value == 0 for value in rung.cost.model_dump().values())


def test_latency_nearest_rank_p95_can_be_below_maximum() -> None:
    rung = _evaluate(
        [
            _run(
                calls=[
                    {"latency_ms": latency, "tokens_in": 0, "tokens_out": 0}
                    for latency in range(210, 0, -10)
                ]
            )
        ]
    ).rungs[0]
    assert rung.cost is not None
    assert rung.cost.calls == 21
    assert rung.cost.latency_p50_ms == 110
    assert rung.cost.latency_p95_ms == 200
    assert rung.cost.latency_max_ms == 210


def test_recall_counts_labels_once_and_precision_counts_events() -> None:
    rung = _evaluate([_run(admitted=[_event(), _event(), _event("unlabelled")])]).rungs[
        0
    ]
    assert (rung.recall.successes, rung.recall.trials) == (1, 1)
    assert (rung.precision.successes, rung.precision.trials) == (2, 3)


def test_mode_summaries_aggregate_cases_but_case_findings_stay_separate() -> None:
    result = _evaluate(
        [
            _run(window_tokens=100),
            _run(case_id="other", mode="chunked", window_tokens=100, admitted=[]),
        ],
        [_case(), _case("other")],
    )
    rung = result.rungs[0]
    assert [(m.mode.value, m.runs, m.recall.rate) for m in rung.modes] == [
        ("chunked", 1, 0.0),
        ("whole", 1, 1.0),
    ]
    assert all(c.single_mode_where_both_allowed for c in rung.per_case)
    assert all(not c.whole_chunked_disagree for c in rung.per_case)
    assert result.falsifiers == (
        "A/case: evaluated in one mode where its window allows both",
        "A/other: evaluated in one mode where its window allows both",
    )


def test_positive_control_only_rung_has_no_scored_cases_or_metrics() -> None:
    rung = _evaluate([_run(removed_label_ids=["p"], admitted=[])]).rungs[0]
    assert rung.runs == 0
    assert rung.per_case == rung.modes == rung.controls == rung.served_model_ids == ()
    assert rung.recall.trials == rung.precision.trials == rung.next_action.trials == 0
    assert not rung.next_action_met
    assert rung.cost is None


@pytest.mark.parametrize(
    ("error", "message"),
    [
        ("unknown-case", "run names unknown case"),
        ("unknown-control-label", "names unknown"),
        ("duplicate-run", "duplicate run"),
        ("unknown-already-done", "outside case already_done"),
    ],
)
def test_request_validation_errors(error: str, message: str) -> None:
    case = _rich_case()
    runs = [_run()]
    if error == "unknown-case":
        runs[0]["case_id"] = "missing"
    elif error == "unknown-control-label":
        case["span_controls"][0]["controls"] = "missing"
    elif error == "duplicate-run":
        runs.append(_run())
    else:
        runs[0]["proposed_already_done"] = ["missing"]
    with pytest.raises(ValidationError, match=message):
        ModelRoundOneEvalRequest.model_validate({"cases": [case], "runs": runs})


def test_contract_imports_topics_and_project_entry_point() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    (route,) = contract["handler_routing"]["handlers"]
    declaration = route["handler"]
    assert declaration["module"] == f"{PACKAGE}.handlers.handler_round_one_eval"
    assert declaration["name"] == "HandlerRoundOneEval"
    assert (
        getattr(importlib.import_module(declaration["module"]), declaration["name"])
        is HandlerRoundOneEval
    )
    for key, expected in (
        ("input_model", ModelRoundOneEvalRequest),
        ("output_model", ModelRoundOneEvalResult),
    ):
        module_name, class_name = route[key].rsplit(".", 1)
        assert getattr(importlib.import_module(module_name), class_name) is expected
    assert (
        "onex.cmd.omnimarket.context-round-one-eval-requested.v1"
        in contract["event_bus"]["subscribe_topics"]
    )
    assert (
        "onex.evt.omnimarket.context-round-one-eval-completed.v1"
        in contract["event_bus"]["publish_topics"]
    )
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["entry-points"]["onex.nodes"][NODE_NAME] == PACKAGE


def _fixture_span(label: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": label["event_id"],
        "same_content_event_ids": label.get("same_content_event_ids", []),
        "start": label["start"],
        "end": label["end"],
    }


def _convert_fixture_case(case: dict[str, Any]) -> ModelCaseLabels:
    """Project TE.1 labels to the text-free scorer input without dropping scoring data."""
    controls = case["negative_controls"]
    return ModelCaseLabels.model_validate(
        {
            "case_id": case["case_id"],
            "transcript_tokens": case["size"]["transcript_tokens_estimate"],
            "propositions": [
                {
                    "label_id": p["label_id"],
                    "kind": p["kind"],
                    "span": _fixture_span(p),
                    "hard_gate": p["t02_statement_id"] is not None,
                }
                for p in case["propositions"]
            ],
            "span_controls": [
                {
                    "label_id": c["label_id"],
                    "control_kind": c["control_kind"],
                    "controls": c["controls"],
                    "span": _fixture_span(c),
                }
                for c in controls
                if c["control_kind"] != "corrected_ruling"
            ],
            "corrected_rulings": [
                {key: c[key] for key in ("label_id", "ruling", "correction")}
                for c in controls
                if c["control_kind"] == "corrected_ruling"
            ],
            "negative_segments": [
                {"label_id": s["label_id"], "span": _fixture_span(s)}
                for s in case["negative_segments"]
            ],
            "already_done": [item["item"] for item in case["already_done"]],
        }
    )


def test_te1_fixture_perfect_synthetic_runs() -> None:
    fixture = yaml.safe_load(
        (ROOT / "tests/fixtures/context_compaction/labels.yaml").read_text(
            encoding="utf-8"
        )
    )
    cases = [_convert_fixture_case(case) for case in fixture["cases"]]
    runs = [
        _run(case_id=case.case_id, admitted=_perfect_events(case.model_dump()))
        for case in cases
    ]
    result = _evaluate(runs, [case.model_dump() for case in cases])
    rung = result.rungs[0]
    assert rung.recall.rate == rung.precision.rate == 1.0
    assert rung.hard_gate_met
    assert rung.negative_segment_admissions == 0
    assert len(rung.per_case) == len(cases) == len(fixture["cases"])
    assert rung.recall.trials == sum(len(c["propositions"]) for c in fixture["cases"])
    assert len(rung.controls) == sum(
        len(c["negative_controls"]) for c in fixture["cases"]
    )
    assert all(c.control_violations == 0 and c.hard_gate_met for c in rung.per_case)
    assert all(c.violations == 0 and c.runs_scored == 1 for c in rung.controls)
    for original, converted in zip(fixture["cases"], cases, strict=True):
        assert len(converted.negative_segments) == len(original["negative_segments"])
        assert converted.already_done == tuple(
            item["item"] for item in original["already_done"]
        )
        for label, proposition in zip(
            original["propositions"], converted.propositions, strict=True
        ):
            assert proposition.span.same_content_event_ids == tuple(
                label.get("same_content_event_ids", [])
            )
            assert proposition.hard_gate == (label["t02_statement_id"] is not None)
    assert result.falsifiers == ()


def test_output_order_and_scores_are_independent_of_input_order() -> None:
    cases = [_rich_case(), {**_rich_case(), "case_id": "another"}]
    runs = [
        _run(
            rung_id=rung,
            case_id=case["case_id"],
            mode=mode,
            admitted=_perfect_events(case),
        )
        for rung in ("B", "A")
        for case in cases
        for mode in ("whole", "chunked")
    ]
    result = _evaluate(runs, cases)
    permuted = [
        {
            **case,
            "propositions": list(reversed(case["propositions"])),
            "span_controls": list(reversed(case["span_controls"])),
        }
        for case in reversed(cases)
    ]
    assert _evaluate(list(reversed(runs)), permuted) == result
    assert [r.rung_id for r in result.rungs] == ["A", "B"]
    for rung in result.rungs:
        assert [c.case_id for c in rung.per_case] == ["another", "case"]
        assert [(c.case_id, c.label_id) for c in rung.controls] == sorted(
            (c.case_id, c.label_id) for c in rung.controls
        )
