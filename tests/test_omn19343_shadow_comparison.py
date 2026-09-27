# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shadow-comparison harness (OMN-19343, unified plan row G3).

A sample of real delegation prompts is answered by two rungs, both answers are
graded by the same grader, and the two pass-rate distributions are compared as
a paired difference with a confidence interval. The acceptance criteria, each
an executed pair:

* AC1, the valid half: a rung compared against itself reports no significant
  difference at the stated confidence;
* AC2, the known-bad half: a rung compared against a deliberately degraded copy
  of itself reports the difference;
* AC3: a comparison with fewer prompts than its power analysis requires is
  refused, naming the required n.

Significance is decided by the exact two-sided McNemar test on the discordant
pairs, which never exceeds its nominal false-difference rate. The number of
prompts is sized against the worst case for that test (every pair discordant),
and the sizing claim is itself asserted by exact computation below.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.delegation.shadow_comparison import (
    ModelShadowPrompt,
    ModelShadowRungAnswer,
    grade_like_the_local_path,
    read_shadow_prompts,
    run_shadow_comparison,
)
from omnimarket.models.ranges import (
    EnumComparisonVerdict,
    EnumIncompleteRunTreatment,
    EnumRangeSampleOutcome,
    ModelComparisonMethod,
    ModelComparisonPair,
)
from omnimarket.ranges import (
    compare_paired_outcomes,
    exact_comparison_power,
    exact_false_difference_rate,
    mcnemar_exact_p_value,
    required_comparison_size,
)

pytestmark = pytest.mark.unit

_MARGIN = 0.2
_CONFIDENCE = 0.95
_POWER = 0.8

P = EnumRangeSampleOutcome.PASS
F = EnumRangeSampleOutcome.FAIL
INC = EnumRangeSampleOutcome.INCOMPLETE


def _method(n: int | None = None) -> ModelComparisonMethod:
    required = required_comparison_size(
        margin=_MARGIN, confidence=_CONFIDENCE, power=_POWER
    )
    return ModelComparisonMethod(
        sample_size=required if n is None else n,
        confidence=_CONFIDENCE,
        power=_POWER,
        margin=_MARGIN,
        incomplete_run_treatment=EnumIncompleteRunTreatment.COUNT_AS_FAILURE,
    )


def _pairs(
    *, both: int, a_only: int, b_only: int, neither: int, b_incomplete: int = 0
) -> list[ModelComparisonPair]:
    shape = (
        [(P, P)] * both
        + [(P, F)] * a_only
        + [(F, P)] * b_only
        + [(F, F)] * neither
        + [(P, INC)] * b_incomplete
    )
    return [
        ModelComparisonPair(case_id=f"case-{index:04d}", outcome_a=a, outcome_b=b)
        for index, (a, b) in enumerate(shape)
    ]


class TestSizing:
    def test_the_size_for_the_default_line(self) -> None:
        # Worst case for the paired test is every pair discordant; the normal
        # approximation there gives 194 at margin 0.2, and the exact search
        # never returns less than its starting point.
        n = required_comparison_size(
            margin=_MARGIN, confidence=_CONFIDENCE, power=_POWER
        )
        assert n >= 194

    @pytest.mark.parametrize("discordance", [0.25, 0.5, 0.75, 1.0])
    def test_the_sized_n_has_the_stated_power_at_every_discordance(
        self, discordance: float
    ) -> None:
        n = required_comparison_size(
            margin=_MARGIN, confidence=_CONFIDENCE, power=_POWER
        )
        power = exact_comparison_power(
            n=n,
            discordance=discordance,
            difference=_MARGIN,
            confidence=_CONFIDENCE,
        )
        assert power >= _POWER, (n, discordance, power)

    @pytest.mark.parametrize("discordance", [0.1, 0.5, 1.0])
    def test_a_rung_equal_to_itself_is_called_different_at_most_alpha(
        self, discordance: float
    ) -> None:
        n = required_comparison_size(
            margin=_MARGIN, confidence=_CONFIDENCE, power=_POWER
        )
        rate = exact_false_difference_rate(
            n=n, discordance=discordance, confidence=_CONFIDENCE
        )
        assert rate <= 1.0 - _CONFIDENCE, (discordance, rate)

    def test_a_smaller_margin_needs_more_prompts(self) -> None:
        wide = required_comparison_size(margin=0.3, confidence=0.95, power=0.8)
        narrow = required_comparison_size(margin=0.1, confidence=0.95, power=0.8)
        assert narrow > wide

    def test_mcnemar_is_one_with_no_discordant_pairs(self) -> None:
        assert mcnemar_exact_p_value(a_only=0, b_only=0) == 1.0

    def test_mcnemar_textbook_value(self) -> None:
        # 0 against 10 discordant: two-sided 2 * 0.5**10.
        assert mcnemar_exact_p_value(a_only=10, b_only=0) == pytest.approx(2 * 0.5**10)


class TestComparison:
    def test_ac1_a_rung_against_itself_reports_no_difference(self) -> None:
        n = _method().sample_size
        pairs = _pairs(both=n - 40, a_only=0, b_only=0, neither=40)
        result = compare_paired_outcomes("self", pairs, _method())
        assert result.verdict is EnumComparisonVerdict.NO_DIFFERENCE, result.reasons
        assert result.difference == 0.0
        assert result.interval_low <= 0.0 <= result.interval_high

    def test_ac1_sampling_noise_alone_is_not_a_difference(self) -> None:
        # A stochastic rung disagrees with itself on some prompts, both ways.
        n = _method().sample_size
        pairs = _pairs(both=n - 60, a_only=11, b_only=9, neither=40)
        result = compare_paired_outcomes("self-noisy", pairs, _method())
        assert result.verdict is EnumComparisonVerdict.NO_DIFFERENCE, result.reasons
        assert result.p_value is not None
        assert result.p_value > 0.05

    def test_ac2_a_degraded_rung_is_reported_different(self) -> None:
        n = _method().sample_size
        pairs = _pairs(both=40, a_only=n - 50, b_only=2, neither=8)
        result = compare_paired_outcomes("degraded", pairs, _method())
        assert result.verdict is EnumComparisonVerdict.DIFFERENCE, result.reasons
        assert result.difference < 0.0
        assert result.interval_high < 0.0

    def test_ac3_fewer_prompts_than_the_power_analysis_is_refused(self) -> None:
        required = _method().sample_size
        pairs = _pairs(both=10, a_only=0, b_only=0, neither=10)
        result = compare_paired_outcomes("small", pairs, _method())
        assert result.verdict is EnumComparisonVerdict.REFUSED
        assert any(f"required n={required}" in reason for reason in result.reasons)

    def test_a_method_not_sized_by_the_power_analysis_is_refused(self) -> None:
        n = _method().sample_size
        pairs = _pairs(both=n, a_only=0, b_only=0, neither=0)
        result = compare_paired_outcomes("undersized-line", pairs, _method(n=20))
        assert result.verdict is EnumComparisonVerdict.REFUSED
        assert any("not sized by the power analysis" in r for r in result.reasons)

    def test_an_empty_comparison_is_refused_not_passed(self) -> None:
        result = compare_paired_outcomes("empty", [], _method())
        assert result.verdict is EnumComparisonVerdict.REFUSED

    def test_an_incomplete_answer_counts_as_a_failure_and_is_counted(self) -> None:
        n = _method().sample_size
        pairs = _pairs(both=n - 30, a_only=0, b_only=0, neither=0, b_incomplete=30)
        result = compare_paired_outcomes("incomplete", pairs, _method())
        assert result.incomplete_b == 30
        assert result.passes_b == n - 30
        assert result.verdict is EnumComparisonVerdict.DIFFERENCE

    def test_a_duplicate_case_is_refused(self) -> None:
        pair = ModelComparisonPair(case_id="dup", outcome_a=P, outcome_b=P)
        with pytest.raises(ValueError, match="duplicate"):
            compare_paired_outcomes("dup", [pair, pair], _method())


def _store(tmp_path: Path, rows: list[tuple[int, str, str, str, str]]) -> Path:
    path = tmp_path / "delegation.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        "create table delegation_events (id integer primary key, correlation_id "
        "text, task_type text, prompt_text text, response_text text)"
    )
    connection.executemany("insert into delegation_events values (?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()
    return path


class TestPromptSource:
    def test_prompts_are_read_from_the_store_and_rows_without_text_skipped(
        self, tmp_path: Path
    ) -> None:
        path = _store(
            tmp_path,
            [
                (1, "c1", "document", "Write a short note.", "A note."),
                (2, "c2", "document", None, None),  # type: ignore[list-item]
                (3, "c3", "reasoning", "Why is the sky blue?", "Rayleigh."),
            ],
        )
        prompts = read_shadow_prompts(path, limit=10, selection_seed=1)
        assert {p.correlation_id for p in prompts} == {"c1", "c3"}
        recorded = {p.correlation_id: p.recorded_response for p in prompts}
        assert recorded == {"c1": "A note.", "c3": "Rayleigh."}

    def test_the_selection_is_deterministic_and_filterable(
        self, tmp_path: Path
    ) -> None:
        rows = [
            (i, f"c{i}", "document" if i % 2 else "reasoning", f"p{i}", f"r{i}")
            for i in range(1, 41)
        ]
        path = _store(tmp_path, rows)
        first = read_shadow_prompts(path, limit=5, selection_seed=7)
        again = read_shadow_prompts(path, limit=5, selection_seed=7)
        assert first == again
        only = read_shadow_prompts(
            path, limit=50, selection_seed=7, task_types=("reasoning",)
        )
        assert {p.task_type for p in only} == {"reasoning"}
        assert len(only) == 20

    def test_an_empty_store_read_is_refused(self, tmp_path: Path) -> None:
        path = _store(tmp_path, [])
        with pytest.raises(ValueError, match="no prompts"):
            read_shadow_prompts(path, limit=5, selection_seed=1)


class TestHarness:
    def _prompts(self, count: int) -> list[ModelShadowPrompt]:
        return [
            ModelShadowPrompt(
                correlation_id=f"c{i:04d}",
                task_type="document",
                prompt=f"prompt {i}",
                recorded_response=None,
            )
            for i in range(count)
        ]

    def test_both_arms_are_graded_by_the_same_grader(self) -> None:
        seen: list[tuple[str, str]] = []

        def grader(prompt: ModelShadowPrompt, content: str) -> tuple[float, float]:
            seen.append((prompt.correlation_id, content))
            return (1.0 if content == "good" else 0.0, 0.8)

        prompts = self._prompts(_method().sample_size)
        result = run_shadow_comparison(
            "same-grader",
            prompts,
            rung_a=lambda _prompt: ModelShadowRungAnswer(content="good"),
            rung_b=lambda _prompt: ModelShadowRungAnswer(content="good"),
            grader=grader,
            method=_method(),
        )
        assert len(seen) == 2 * len(prompts)
        assert result.verdict is EnumComparisonVerdict.NO_DIFFERENCE

    def test_a_transport_failure_is_incomplete_never_graded(self) -> None:
        graded: list[str] = []

        def grader(prompt: ModelShadowPrompt, content: str) -> tuple[float, float]:
            graded.append(content)
            return (1.0, 0.8)

        prompts = self._prompts(_method().sample_size)
        result = run_shadow_comparison(
            "transport",
            prompts,
            rung_a=lambda _prompt: ModelShadowRungAnswer(content="good"),
            rung_b=lambda _prompt: ModelShadowRungAnswer(content=None),
            grader=grader,
            method=_method(),
        )
        assert result.incomplete_b == len(prompts)
        assert len(graded) == len(prompts)
        assert result.verdict is EnumComparisonVerdict.DIFFERENCE

    def test_the_real_gate_scores_a_refusal_below_an_answer(self) -> None:
        prompt = ModelShadowPrompt(
            correlation_id="c1",
            task_type="document",
            prompt="Explain in two paragraphs why a write-ahead log makes a "
            "database crash-safe.",
            recorded_response=None,
        )
        answer = (
            "A write-ahead log records every change before it is applied to the "
            "data files, so after a crash the database replays the log to "
            "restore any committed change that had not reached the data files.\n\n"
            "Because the log is appended sequentially and flushed at commit, a "
            "transaction is durable once its log record is on disk, and a torn "
            "page in the data files can be rebuilt from the log on restart."
        )
        good_score, bar = grade_like_the_local_path(prompt, answer)
        bad_score, _ = grade_like_the_local_path(prompt, "")
        assert good_score is not None
        assert bad_score is not None
        assert bad_score < good_score
        assert bar is None or 0.0 < bar <= 1.0


class TestCli:
    def test_the_cli_exits_non_zero_when_refused(self, tmp_path: Path) -> None:
        pairs = _pairs(both=5, a_only=0, b_only=0, neither=5)
        pairs_path = tmp_path / "pairs.json"
        pairs_path.write_text(
            json.dumps([pair.model_dump(mode="json") for pair in pairs]),
            encoding="utf-8",
        )
        method_path = tmp_path / "method.json"
        method_path.write_text(_method().model_dump_json(), encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnimarket.ranges",
                "compare",
                "--method",
                str(method_path),
                "--pairs",
                str(pairs_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 2, completed.stdout + completed.stderr
        assert "REFUSED" in completed.stdout
