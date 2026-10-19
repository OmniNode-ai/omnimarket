# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hostile reviewer eval scores recall and false blocks and fails a drop (OMN-17427)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_reviewer_eval_effect.__main__ import (
    DEFAULT_CASES,
    load_case_set,
    main,
)
from omnimarket.nodes.node_reviewer_eval_effect.handlers.adapter_publisher import (
    scored_topic,
)
from omnimarket.nodes.node_reviewer_eval_effect.handlers.handler_reviewer_eval import (
    HandlerReviewerEval,
    verdict_from_review_result,
)
from omnimarket.nodes.node_reviewer_eval_effect.models import (
    EnumReviewerEvalLabel,
    EnumReviewerVerdict,
    ModelReviewerEvalCaseSet,
    ModelReviewerEvalObservation,
    ModelReviewerEvalRequest,
)

pytestmark = pytest.mark.unit

CASES = load_case_set(DEFAULT_CASES)


def _request(
    verdicts: dict[str, EnumReviewerVerdict],
    *,
    case_set: ModelReviewerEvalCaseSet = CASES,
    prior: ModelReviewerEvalCaseSet | None = None,
) -> ModelReviewerEvalRequest:
    return ModelReviewerEvalRequest(
        correlation_id="test",
        reviewer_ref="test",
        source="live",
        case_set=case_set,
        observations=tuple(
            ModelReviewerEvalObservation(case_id=case_id, verdict=verdict)
            for case_id, verdict in verdicts.items()
        ),
        prior_case_set=prior,
    )


def _recorded() -> dict[str, EnumReviewerVerdict]:
    return {case.case_id: case.recorded_verdict for case in CASES.cases}


def _with(**changes: object) -> ModelReviewerEvalCaseSet:
    raw = CASES.model_dump(mode="json")
    raw.update(changes)
    return ModelReviewerEvalCaseSet.model_validate(raw)


def test_the_set_holds_the_recorded_history() -> None:
    by_id = {case.case_id: case for case in CASES.cases}
    overturned = by_id["omnimarket-4063-d25be197"]
    assert overturned.label is EnumReviewerEvalLabel.OVERTURNED_BLOCK
    assert overturned.head_sha.startswith("d25be197")
    labels = [case.label for case in CASES.cases]
    assert labels.count(EnumReviewerEvalLabel.TRUE_CATCH) >= 2
    assert labels.count(EnumReviewerEvalLabel.KNOWN_CLEAN) >= 2


def test_the_recorded_reviewer_scores_the_baseline() -> None:
    scored = HandlerReviewerEval().handle(_request(_recorded()))
    assert (scored.caught, scored.true_catch_cases) == (2, 2)
    assert (scored.false_blocks, scored.clean_cases) == (2, 8)
    assert scored.recall == pytest.approx(CASES.baseline.recall)
    assert scored.false_block_rate == pytest.approx(CASES.baseline.false_block_rate)
    assert not scored.regressed, scored.reasons


def test_a_reviewer_that_stops_overturned_blocks_scores_better_and_holds() -> None:
    verdicts = _recorded()
    verdicts["omnimarket-4063-d25be197"] = EnumReviewerVerdict.PASSED
    scored = HandlerReviewerEval().handle(_request(verdicts))
    assert scored.false_block_rate == pytest.approx(1 / 8)
    assert not scored.regressed


def test_a_missed_true_catch_regresses() -> None:
    verdicts = _recorded()
    verdicts["omnimarket-3943-e2816be3"] = EnumReviewerVerdict.PASSED
    scored = HandlerReviewerEval().handle(_request(verdicts))
    assert scored.recall == pytest.approx(0.5)
    assert scored.regressed
    assert any("recall" in reason for reason in scored.reasons)


def test_a_new_false_block_on_a_clean_head_regresses() -> None:
    verdicts = _recorded()
    verdicts["omnimarket-4080-12f2319c"] = EnumReviewerVerdict.BLOCKED
    scored = HandlerReviewerEval().handle(_request(verdicts))
    assert scored.false_block_rate == pytest.approx(3 / 8)
    assert scored.regressed
    assert any("false-block rate" in reason for reason in scored.reasons)


@pytest.mark.parametrize("verdict", [EnumReviewerVerdict.DEGRADED, None])
def test_a_case_without_a_verdict_never_passes(
    verdict: EnumReviewerVerdict | None,
) -> None:
    verdicts = _recorded()
    if verdict is None:
        del verdicts["omnimarket-4047-4bd1cd2a"]
    else:
        verdicts["omnimarket-4047-4bd1cd2a"] = verdict
    scored = HandlerReviewerEval().handle(_request(verdicts))
    assert scored.degraded == 1
    assert scored.regressed
    assert any("omnimarket-4047-4bd1cd2a" in reason for reason in scored.reasons)


def test_a_verdict_for_an_unknown_case_regresses() -> None:
    verdicts = _recorded()
    verdicts["not-a-case"] = EnumReviewerVerdict.PASSED
    assert HandlerReviewerEval().handle(_request(verdicts)).regressed


def test_the_set_cannot_shrink_relabel_or_lower_its_baseline() -> None:
    cases = CASES.model_dump(mode="json")["cases"]
    dropped = _with(
        cases=[c for c in cases if c["case_id"] != "omnimarket-4063-d25be197"]
    )
    lowered = _with(baseline={"recall": 0.5, "false_block_rate": 0.5})
    relabelled_cases = [dict(c) for c in cases]
    for case in relabelled_cases:
        if case["case_id"] == "omnimarket-4063-d25be197":
            case["label"] = "true_catch"
    relabelled = _with(cases=relabelled_cases)

    for changed, word in (
        (dropped, "removed"),
        (lowered, "baseline"),
        (relabelled, "relabelled"),
    ):
        verdicts = {case.case_id: case.recorded_verdict for case in changed.cases}
        scored = HandlerReviewerEval().handle(
            _request(verdicts, case_set=changed, prior=CASES)
        )
        assert scored.regressed
        assert any(word in reason for reason in scored.reasons), scored.reasons


def test_a_grown_set_with_a_raised_baseline_holds() -> None:
    grown = _with(baseline={"recall": 1.0, "false_block_rate": 0.25})
    scored = HandlerReviewerEval().handle(
        _request(_recorded(), case_set=grown, prior=CASES)
    )
    assert not scored.regressed


@pytest.mark.parametrize(
    ("true_catches", "match"),
    [(False, "no true catch"), (True, "no clean case")],
)
def test_a_set_without_both_kinds_of_case_is_refused_before_any_division(
    true_catches: bool, match: str
) -> None:
    cases = [
        c
        for c in CASES.model_dump(mode="json")["cases"]
        if (c["label"] == "true_catch") is true_catches
    ]
    with pytest.raises(ValidationError, match=match):
        _with(cases=cases)


def test_a_block_case_must_carry_its_recorded_block() -> None:
    cases = CASES.model_dump(mode="json")["cases"]
    cases[0]["recorded_verdict"] = "passed"
    with pytest.raises(ValidationError, match="recorded block"):
        _with(cases=cases)


@pytest.mark.parametrize(
    ("document", "verdict"),
    [
        ({"quorum": {"verdict": "blocked"}, "models_succeeded": ["a", "b"]}, "blocked"),
        ({"quorum": {"verdict": "passed"}, "models_succeeded": ["a", "b"]}, "passed"),
        (
            {"quorum": {"verdict": "degraded_quorum"}, "models_succeeded": ["a"]},
            "degraded",
        ),
        ({"models_succeeded": []}, "degraded"),
        ({"models_succeeded": ["a", "b"]}, "passed"),
    ],
)
def test_the_verdict_is_read_the_way_the_gate_reads_it(
    document: dict[str, object], verdict: str
) -> None:
    assert verdict_from_review_result(document) is EnumReviewerVerdict(verdict)


def test_the_cli_scores_live_results_and_publishes_the_score(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KAFKA_BROKERS", raising=False)
    for case in CASES.cases:
        verdict = "blocked" if case.should_block else "passed"
        (tmp_path / f"{case.case_id}.json").write_text(
            json.dumps(
                {"quorum": {"verdict": verdict}, "models_succeeded": ["a", "b"]}
            ),
            encoding="utf-8",
        )
    assert main(["--reviewer-ref", "pin-x", "--observed-dir", str(tmp_path)]) == 0
    out, err = capsys.readouterr()
    scored = json.loads(out)
    assert (scored["source"], scored["recall"], scored["false_block_rate"]) == (
        "live",
        1.0,
        0.0,
    )
    published = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
    assert (
        published[0]["topic"]
        == scored_topic()
        == "onex.evt.omnimarket.reviewer-eval-scored.v1"
    )
    assert published[0]["event"]["reviewer_ref"] == "pin-x"


def test_the_cli_fails_a_live_run_that_misses_a_catch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KAFKA_BROKERS", raising=False)
    for case in CASES.cases:
        (tmp_path / f"{case.case_id}.json").write_text(
            json.dumps(
                {"quorum": {"verdict": "passed"}, "models_succeeded": ["a", "b"]}
            ),
            encoding="utf-8",
        )
    assert main(["--reviewer-ref", "x", "--observed-dir", str(tmp_path)]) == 1
    assert "REGRESSION: recall" in capsys.readouterr().err


def test_the_cli_refuses_a_lowered_baseline_against_the_base_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KAFKA_BROKERS", raising=False)
    raw = yaml.safe_load(DEFAULT_CASES.read_text(encoding="utf-8"))
    raw["baseline"]["false_block_rate"] = 0.5
    lowered = tmp_path / "eval_cases.yaml"
    lowered.write_text(yaml.safe_dump(raw), encoding="utf-8")
    argv = ["--reviewer-ref", "x", "--cases", str(lowered)]
    assert main(argv) == 0
    assert main([*argv, "--prior-cases", str(DEFAULT_CASES)]) == 1
    assert main([*argv, "--prior-cases", str(tmp_path / "absent.yaml")]) == 0


def test_the_cli_reports_an_unreadable_set(tmp_path: Path) -> None:
    broken = tmp_path / "eval_cases.yaml"
    broken.write_text("schema_version: nope\n", encoding="utf-8")
    assert main(["--reviewer-ref", "x", "--cases", str(broken)]) == 2
