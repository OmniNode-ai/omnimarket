# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic ruling, answer and review-row scoring cases (OMN-21111).

Every package, prompt, session and token estimate below is synthetic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_context_replay_eval_compute.handlers import (
    HandlerAnswerComparison,
    HandlerReviewRowSizing,
    HandlerRulingPresence,
)
from omnimarket.nodes.node_context_replay_eval_compute.models import (
    EnumAnswerGrade,
    EnumPresence,
    EnumSizingVerdict,
    ModelAnswerComparisonRequest,
    ModelBoundaryCarry,
    ModelCarriedPrompt,
    ModelCaseAnswer,
    ModelEvaluationRun,
    ModelExpectedQuote,
    ModelPresenceCase,
    ModelReviewRowSizingRequest,
    ModelRulingPresenceRequest,
)

pytestmark = pytest.mark.unit


def at(minute: int) -> datetime:
    return datetime(2025, 1, 1, 0, minute, tzinfo=UTC)


def prompt(prompt_id: str, minute: int, tokens: int) -> ModelCarriedPrompt:
    return ModelCarriedPrompt(
        prompt_id=prompt_id, captured_at=at(minute), token_estimate=tokens
    )


def boundary(
    boundary_id: str = "b1",
    *,
    minute: int = 10,
    session: str = "s1",
    other: int = 60,
    prompts: tuple[ModelCarriedPrompt, ...] = (),
    overhead: int = 10,
    control: bool = False,
    override: int | None = None,
) -> ModelBoundaryCarry:
    return ModelBoundaryCarry(
        session_id=session,
        boundary_id=boundary_id,
        boundary_at=at(minute),
        other_required_tokens={"RULINGS": other},
        operator_words_overhead_tokens=overhead,
        carried_prompts=prompts,
        control=control,
        budget_override_tokens=override,
    )


def answer(
    case_id: str = "c1",
    candidate_id: str = "candidate-1",
    package: EnumAnswerGrade = EnumAnswerGrade.MATCH,
    native: EnumAnswerGrade = EnumAnswerGrade.MISMATCH,
) -> ModelCaseAnswer:
    return ModelCaseAnswer(
        case_id=case_id,
        candidate_id=candidate_id,
        package_answer=package,
        native_answer=native,
    )


class TestPresence:
    @pytest.mark.parametrize(
        ("text", "quote", "spans", "expected"),
        [
            (
                "retain synthetic decision",
                "synthetic decision",
                (),
                EnumPresence.PRESENT,
            ),
            (
                "retain 'synthetic' \"decision\"",
                "retain \u2018synthetic\u2019 \u201cdecision\u201d",
                (),
                EnumPresence.PRESENT,
            ),
            (
                "retain \u2018synthetic\u2019 \u201cdecision\u201d",
                "retain 'synthetic' \"decision\"",
                (),
                EnumPresence.PRESENT,
            ),
            ("retain synthetic decision", "missing", (), EnumPresence.MISSING),
            (
                "retain synthetic decision",
                None,
                ("retain", "decision"),
                EnumPresence.PRESENT,
            ),
            (
                "retain 'synthetic'",
                None,
                ("\u2018synthetic\u2019",),
                EnumPresence.PRESENT,
            ),
            (
                "retain synthetic decision",
                None,
                ("retain", "missing"),
                EnumPresence.MISSING,
            ),
            ("retain synthetic decision", None, (), EnumPresence.UNSCORABLE),
            (
                "retain synthetic decision",
                "Synthetic decision",
                (),
                EnumPresence.MISSING,
            ),
            (
                "retain  synthetic decision",
                "retain synthetic",
                (),
                EnumPresence.MISSING,
            ),
            ("retain synthetic decision", "missing", ("retain",), EnumPresence.MISSING),
            ("", "", (), EnumPresence.PRESENT),
        ],
    )
    def test_exact_words(
        self,
        text: str,
        quote: str | None,
        spans: tuple[str, ...],
        expected: EnumPresence,
    ) -> None:
        result = HandlerRulingPresence().handle(
            ModelRulingPresenceRequest(
                cases=(
                    ModelPresenceCase(
                        case_id="c1",
                        candidate_id="candidate-1",
                        package_text=text,
                        expected=(
                            ModelExpectedQuote(
                                statement_id="r1", quote=quote, required_spans=spans
                            ),
                        ),
                    ),
                )
            )
        )
        assert result.cases_total == 1
        assert result.cases[0].candidate_id == "candidate-1"
        assert result.cases[0].statements[0].presence is expected
        assert result.cases[0].passed is (expected is EnumPresence.PRESENT)
        assert result.cases_passed == int(expected is EnumPresence.PRESENT)
        assert result.all_present is (expected is EnumPresence.PRESENT)
        assert result.falsifiers == (
            ()
            if expected is EnumPresence.PRESENT
            else (f"case c1: statement r1 {expected}",)
        )

    def test_all_cases_must_pass_and_falsifiers_keep_input_order(self) -> None:
        cases = tuple(
            ModelPresenceCase(
                case_id=cid, package_text="synthetic words", expected=statements
            )
            for cid, statements in (
                ("c2", (ModelExpectedQuote(statement_id="r1", quote="words"),)),
                (
                    "c1",
                    (
                        ModelExpectedQuote(statement_id="r3", quote="absent"),
                        ModelExpectedQuote(
                            statement_id="r2", withheld_reason="synthetic withheld text"
                        ),
                    ),
                ),
            )
        )
        result = HandlerRulingPresence().handle(ModelRulingPresenceRequest(cases=cases))
        assert result.cases_total == 2
        assert result.cases_passed == 1
        assert not result.all_present
        assert result.falsifiers == (
            "case c1: statement r3 MISSING",
            "case c1: statement r2 UNSCORABLE",
        )
        good = HandlerRulingPresence().handle(
            ModelRulingPresenceRequest(
                cases=(cases[0], cases[0].model_copy(update={"case_id": "c3"}))
            )
        )
        assert good.all_present
        assert good.cases_passed == 2
        assert good.falsifiers == ()


class TestAnswers:
    @pytest.mark.parametrize(
        ("package", "native"),
        [
            (EnumAnswerGrade.UNAVAILABLE, EnumAnswerGrade.UNAVAILABLE),
            (EnumAnswerGrade.UNAVAILABLE, EnumAnswerGrade.MISMATCH),
            (EnumAnswerGrade.UNAVAILABLE, EnumAnswerGrade.MATCH),
            (EnumAnswerGrade.MATCH, EnumAnswerGrade.UNAVAILABLE),
            (EnumAnswerGrade.MISMATCH, EnumAnswerGrade.UNAVAILABLE),
        ],
    )
    def test_missing_answers_cannot_pass_quality_comparison(
        self, package: EnumAnswerGrade, native: EnumAnswerGrade
    ) -> None:
        result = HandlerAnswerComparison().handle(
            ModelAnswerComparisonRequest(
                runs=tuple(
                    ModelEvaluationRun(
                        run_id=rid,
                        cases=(answer(package=package, native=native),),
                    )
                    for rid in ("r1", "r2")
                )
            )
        )
        # TA.9 requires asking the fixed question against both actual inputs.
        # Zero matches against zero absent answers is not measured parity.
        assert not result.next_action_met
        assert all(not run.package_at_least_native for run in result.runs)
        assert any("unavailable" in failure for failure in result.falsifiers)

    @pytest.mark.parametrize(
        ("package", "native", "met"),
        [
            (EnumAnswerGrade.MATCH, EnumAnswerGrade.MISMATCH, True),
            (EnumAnswerGrade.MATCH, EnumAnswerGrade.MATCH, True),
            (EnumAnswerGrade.MISMATCH, EnumAnswerGrade.MATCH, False),
        ],
    )
    def test_match_counts(
        self, package: EnumAnswerGrade, native: EnumAnswerGrade, met: bool
    ) -> None:
        result = HandlerAnswerComparison().handle(
            ModelAnswerComparisonRequest(
                runs=(
                    ModelEvaluationRun(
                        run_id="r1", cases=(answer(package=package, native=native),)
                    ),
                )
            )
        )
        assert result.next_action_met is met
        assert result.runs[0].package_at_least_native is met
        assert result.runs[0].package_matches == int(package is EnumAnswerGrade.MATCH)
        assert result.runs[0].native_matches == int(native is EnumAnswerGrade.MATCH)
        assert result.runs[0].cases == 1
        assert not result.determinism_checked
        assert not result.candidate_ids_identical
        assert result.candidate_id_mismatches == ()
        assert result.falsifiers == (
            ("run r1: package 0 < native 1",) if not met else ()
        ) + ("determinism not checked: fewer than two runs",)

    def test_unavailable_is_not_a_match(self) -> None:
        run = ModelEvaluationRun(
            run_id="r1",
            cases=(
                answer(native=EnumAnswerGrade.UNAVAILABLE),
                answer(case_id="c2", package=EnumAnswerGrade.UNAVAILABLE),
            ),
        )
        result = HandlerAnswerComparison().handle(
            ModelAnswerComparisonRequest(runs=(run,))
        )
        counts = result.runs[0]
        assert counts.native_unavailable == counts.package_unavailable == 1
        assert counts.native_matches == 0
        assert counts.package_matches == 1
        assert not counts.package_at_least_native
        assert not result.next_action_met
        assert result.falsifiers == (
            "run r1: answers unavailable: package 1, native 1",
            "determinism not checked: fewer than two runs",
        )

    def test_identical_candidates_across_runs(self) -> None:
        runs = tuple(
            ModelEvaluationRun(
                run_id=rid,
                cases=(answer(), answer(case_id="c2", candidate_id="candidate-2")),
            )
            for rid in ("r1", "r2")
        )
        result = HandlerAnswerComparison().handle(
            ModelAnswerComparisonRequest(runs=runs)
        )
        assert result.next_action_met
        assert result.determinism_checked
        assert result.candidate_ids_identical
        assert result.candidate_id_mismatches == result.falsifiers == ()

    def test_differing_and_absent_cases_are_sorted_across_all_runs(self) -> None:
        runs = (
            ModelEvaluationRun(
                run_id="r1", cases=(answer(case_id="z"), answer(case_id="a"))
            ),
            ModelEvaluationRun(
                run_id="r2", cases=(answer(case_id="a"), answer(case_id="z"))
            ),
            ModelEvaluationRun(
                run_id="r3", cases=(answer(case_id="z", candidate_id="changed"),)
            ),
        )
        result = HandlerAnswerComparison().handle(
            ModelAnswerComparisonRequest(runs=runs)
        )
        assert result.determinism_checked
        assert not result.candidate_ids_identical
        assert result.candidate_id_mismatches == ("a", "z")
        assert result.falsifiers == (
            "case a: candidate_id differs across runs",
            "case z: candidate_id differs across runs",
        )

    def test_one_failing_run_fails_next_action(self) -> None:
        result = HandlerAnswerComparison().handle(
            ModelAnswerComparisonRequest(
                runs=(
                    ModelEvaluationRun(run_id="r1", cases=(answer(),)),
                    ModelEvaluationRun(
                        run_id="r2",
                        cases=(
                            answer(
                                package=EnumAnswerGrade.MISMATCH,
                                native=EnumAnswerGrade.MATCH,
                            ),
                        ),
                    ),
                )
            )
        )
        assert not result.next_action_met
        assert result.falsifiers == ("run r2: package 0 < native 1",)

    def test_duplicate_case_id_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="duplicate case_id"):
            ModelEvaluationRun(run_id="r1", cases=(answer(), answer()))

    def test_duplicate_run_id_is_refused(self) -> None:
        run = ModelEvaluationRun(run_id="r1", cases=(answer(),))
        with pytest.raises(ValidationError, match="duplicate run_id"):
            ModelAnswerComparisonRequest(runs=(run, run))


class TestSizing:
    def test_under_and_exactly_at_budget(self) -> None:
        request = ModelReviewRowSizingRequest(
            budget_tokens=100,
            boundaries=(
                boundary(prompts=(prompt("p1", 1, 20),)),
                boundary("b2", other=90),
            ),
        )
        result = HandlerReviewRowSizing().handle(request)
        for sized, total in zip(result.boundaries, (90, 100), strict=True):
            assert sized.verdict is EnumSizingVerdict.FITS_AS_IS
            assert not sized.exceeds_budget
            assert sized.prompts_to_name == sized.tokens_to_name == 0
            assert sized.named_prompt_ids == ()
            assert sized.required_tokens == sized.required_tokens_after_review == total
            assert sized.replay_reproduces
        assert result.boundaries[0].operator_words_tokens == 30

    def test_two_oldest_prompts_out_of_order(self) -> None:
        carried = (
            prompt("newest", 3, 30),
            prompt("middle", 2, 25),
            prompt("oldest", 1, 20),
        )
        request = ModelReviewRowSizingRequest(
            budget_tokens=100, boundaries=(boundary(prompts=carried),)
        )
        before = request.model_dump()
        result = HandlerReviewRowSizing().handle(request)
        sized = result.boundaries[0]
        assert sized.verdict is EnumSizingVerdict.FITS_AFTER_REVIEW
        assert sized.operator_words_tokens == 85
        assert sized.required_tokens == 145
        assert sized.exceeds_budget
        assert sized.prompts_to_name == 2
        assert sized.tokens_to_name == 45
        assert sized.named_prompt_ids == ("oldest", "middle")
        assert sized.required_tokens_after_review == 100
        assert sized.replay_reproduces
        assert result.sessions[0].review_rows == 1
        assert result.falsifiers == ()
        assert request.model_dump() == before

    def test_shortest_prefix_and_tie_break_by_prompt_id(self) -> None:
        sized = (
            HandlerReviewRowSizing()
            .handle(
                ModelReviewRowSizingRequest(
                    budget_tokens=100,
                    boundaries=(
                        boundary(prompts=(prompt("b", 1, 20), prompt("a", 1, 20))),
                    ),
                )
            )
            .boundaries[0]
        )
        assert sized.prompts_to_name == 1
        assert sized.named_prompt_ids == ("a",)
        assert sized.tokens_to_name == 20
        assert sized.required_tokens_after_review == 90
        assert sized.replay_reproduces

    @pytest.mark.parametrize(
        ("other", "overhead", "prompts"),
        [(101, 0, (prompt("p1", 1, 30),)), (95, 10, ()), (101, 0, ())],
    )
    def test_does_not_fit_even_without_prompts(
        self, other: int, overhead: int, prompts: tuple[ModelCarriedPrompt, ...]
    ) -> None:
        result = HandlerReviewRowSizing().handle(
            ModelReviewRowSizingRequest(
                budget_tokens=100,
                boundaries=(boundary(other=other, overhead=overhead, prompts=prompts),),
            )
        )
        sized = result.boundaries[0]
        assert sized.verdict is EnumSizingVerdict.DOES_NOT_FIT
        assert (
            sized.prompts_to_name
            is sized.tokens_to_name
            is sized.required_tokens_after_review
            is None
        )
        assert sized.named_prompt_ids == ()
        assert sized.replay_reproduces
        assert result.sessions[0].review_rows == 0
        assert result.sessions[0].does_not_fit == 1
        assert result.does_not_fit_share == 1.0

    def test_zero_token_prompts_are_part_of_the_oldest_prefix(self) -> None:
        sized = (
            HandlerReviewRowSizing()
            .handle(
                ModelReviewRowSizingRequest(
                    budget_tokens=100,
                    boundaries=(
                        boundary(prompts=(prompt("zero", 1, 0), prompt("p1", 2, 40))),
                    ),
                )
            )
            .boundaries[0]
        )
        assert sized.named_prompt_ids == ("zero", "p1")
        assert sized.prompts_to_name == 2
        assert sized.tokens_to_name == 40
        assert sized.replay_reproduces

    def test_zero_fallback_has_positive_control_and_excludes_it(self) -> None:
        result = HandlerReviewRowSizing().handle(
            ModelReviewRowSizingRequest(
                budget_tokens=100,
                boundaries=(boundary(), boundary("control", control=True, override=50)),
            )
        )
        assert result.zero_fallback_has_control
        assert result.control_boundaries == result.control_exceeding == 1
        assert result.boundaries_total == 1
        assert result.exceeding == result.does_not_fit == 0
        assert result.fallback_share == result.does_not_fit_share == 0.0
        assert result.sessions[0].boundaries == 1
        assert result.sessions[0].does_not_fit == 0
        assert result.boundaries[1].budget_tokens == 50
        assert result.boundaries[1].verdict is EnumSizingVerdict.DOES_NOT_FIT
        assert result.falsifiers == ()

    @pytest.mark.parametrize("control", [False, True])
    def test_zero_fallback_without_exceeding_control_fails(self, control: bool) -> None:
        result = HandlerReviewRowSizing().handle(
            ModelReviewRowSizingRequest(
                budget_tokens=100, boundaries=(boundary(control=control),)
            )
        )
        assert not result.zero_fallback_has_control
        assert result.falsifiers == (
            "zero fallback frequency without a positive control",
        )
        assert result.boundaries_total == int(not control)
        assert result.fallback_share == result.does_not_fit_share == 0.0

    def test_only_positive_control_has_zero_denominator(self) -> None:
        result = HandlerReviewRowSizing().handle(
            ModelReviewRowSizingRequest(
                budget_tokens=100, boundaries=(boundary(control=True, override=50),)
            )
        )
        assert result.boundaries_total == 0
        assert result.sessions == ()
        assert result.fallback_share == result.does_not_fit_share == 0.0
        assert result.zero_fallback_has_control

    @pytest.mark.parametrize(("second_other", "rows"), [(80, 2), (60, 1)])
    def test_session_rows_in_boundary_time_order(
        self, second_other: int, rows: int
    ) -> None:
        prompts = (prompt("p1", 1, 20), prompt("p2", 2, 20))
        first = boundary("z-first", minute=10, prompts=prompts)
        second = boundary("a-second", minute=20, other=second_other, prompts=prompts)
        result = HandlerReviewRowSizing().handle(
            ModelReviewRowSizingRequest(budget_tokens=100, boundaries=(second, first))
        )
        assert [item.boundary_id for item in result.boundaries] == [
            "a-second",
            "z-first",
        ]
        assert result.sessions[0].review_rows == rows
        assert result.sessions[0].exceeding == 2

    def test_shares_and_sorted_sessions(self) -> None:
        result = HandlerReviewRowSizing().handle(
            ModelReviewRowSizingRequest(
                budget_tokens=100,
                boundaries=(
                    boundary("b1", session="z", prompts=(prompt("p1", 1, 40),)),
                    boundary("b2", session="a", other=110),
                    boundary("b3", session="a"),
                    boundary("b4", session="a", control=True, override=30),
                ),
            )
        )
        assert result.boundaries_total == 3
        assert result.exceeding == 2
        assert result.fallback_share == 0.6667
        assert result.does_not_fit == 1
        assert result.does_not_fit_share == 0.3333
        assert [item.session_id for item in result.sessions] == ["a", "z"]
        assert result.sessions[0].boundaries == 2
        assert result.sessions[0].does_not_fit == 1
        assert result.sessions[0].review_rows == 0
        assert result.zero_fallback_has_control

    @pytest.mark.parametrize(
        ("changes", "message"),
        [
            (
                {"other_required_tokens": {"OPERATOR_WORDS_UNRECORDED": 10}},
                "OPERATOR_WORDS_UNRECORDED",
            ),
            ({"other_required_tokens": {"RULINGS": -1}}, "greater than or equal"),
            ({"budget_override_tokens": 50}, "requires a control"),
            ({"control": True, "budget_override_tokens": 0}, "greater than 0"),
            ({"operator_words_overhead_tokens": -1}, "greater than or equal"),
            (
                {"carried_prompts": (prompt("p1", 1, 10), prompt("p1", 2, 20))},
                "duplicate prompt_id",
            ),
        ],
    )
    def test_invalid_boundary_is_refused(
        self, changes: dict[str, Any], message: str
    ) -> None:
        data = boundary().model_dump() | changes
        with pytest.raises(ValidationError, match=message):
            ModelBoundaryCarry.model_validate(data)

    def test_duplicate_boundaries_refused_but_ids_can_repeat_across_sessions(
        self,
    ) -> None:
        with pytest.raises(ValidationError, match="duplicate"):
            ModelReviewRowSizingRequest(
                budget_tokens=100, boundaries=(boundary(), boundary())
            )
        request = ModelReviewRowSizingRequest(
            budget_tokens=100, boundaries=(boundary(), boundary(session="s2"))
        )
        assert len(request.boundaries) == 2

    @pytest.mark.parametrize(
        "changes", [{"token_estimate": -1}, {"captured_at": "2025-01-01T00:00:00"}]
    )
    def test_invalid_prompt_is_refused(self, changes: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            ModelCarriedPrompt.model_validate(
                prompt("p1", 1, 10).model_dump() | changes
            )


@pytest.mark.parametrize(
    ("model", "data"),
    [
        (ModelRulingPresenceRequest, {"cases": []}),
        (
            ModelPresenceCase,
            {"case_id": "c1", "package_text": "synthetic", "expected": []},
        ),
        (ModelEvaluationRun, {"run_id": "r1", "cases": []}),
        (ModelAnswerComparisonRequest, {"runs": []}),
        (ModelReviewRowSizingRequest, {"budget_tokens": 100, "boundaries": []}),
        (ModelReviewRowSizingRequest, {"budget_tokens": 0, "boundaries": [boundary()]}),
    ],
)
def test_empty_or_invalid_requests_fail_closed(
    model: Any, data: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(data)


def test_models_are_frozen_and_reject_extra_fields() -> None:
    expected = ModelExpectedQuote(statement_id="r1", quote="synthetic")
    with pytest.raises(ValidationError, match="frozen"):
        expected.quote = "changed"
    with pytest.raises(ValidationError, match="extra"):
        ModelExpectedQuote.model_validate({"statement_id": "r1", "extra": "synthetic"})
