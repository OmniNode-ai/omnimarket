# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The range evaluator (OMN-19320, unified plan row G1, section 2b).

A range check judges a sampled outcome (model output) as a distribution against
a floor over n runs, and it BLOCKS when its acceptance line is missed. Section
2b's method, which every assertion below pins:

* the acceptance line is declared before the runs, with four parts: n sized by
  a power analysis, the confidence level, the power at a stated margin, and the
  incomplete-run treatment;
* the result is a bootstrap interval, and the line is met only when the
  one-sided lower bound clears the floor, so a small n cannot pass by luck;
* an evaluation with fewer samples than the power-analysed n is refused, naming
  the required n (AC3);
* a seed-pinned run, a forced temperature or a retry-until-green run is refused
  as a range result (AC4);
* a missing, cancelled or incomplete run counts as a failure and is never
  excluded.

Every refusal and every miss exits non-zero through the CLI, because a range
check blocks (ruling 2026-09-23T16:10:08Z). The pair of AC2 is here as two
corpora: one known to miss its floor, refused, and one known to meet it, passed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.models.ranges import (
    EnumIncompleteRunTreatment,
    EnumRangeSampleOutcome,
    EnumRangeVerdict,
    ModelRangeAcceptanceLine,
    ModelRangeMethod,
    ModelRangeRun,
    ModelRangeSample,
)
from omnimarket.ranges import evaluate_range_line, required_sample_size

pytestmark = pytest.mark.unit

#: floor 0.8, margin 0.1, one-sided 95 percent, power 0.8. The power analysis
#: sizes this at 83 (asserted below), so every corpus here uses that n.
_FLOOR = 0.8
_MARGIN = 0.1
_CONFIDENCE = 0.95
_POWER = 0.8
_N = 83


def _line(*, n: int = _N, case_ids: tuple[str, ...] = ()) -> ModelRangeAcceptanceLine:
    return ModelRangeAcceptanceLine(
        check_id="delegation.response_quality.example",
        case_set="every local delegation of the document class",
        declared_case_ids=case_ids,
        floor=_FLOOR,
        window="the last 83 delegations",
        method=ModelRangeMethod(
            sample_size=n,
            confidence=_CONFIDENCE,
            power=_POWER,
            margin=_MARGIN,
            incomplete_run_treatment=EnumIncompleteRunTreatment.COUNT_AS_FAILURE,
        ),
    )


def _run(
    run_id: str,
    *,
    passes: int,
    fails: int = 0,
    incomplete: int = 0,
    sampling_seed: int | None = None,
    temperature_forced: bool = False,
    retried_until_pass: bool = False,
) -> ModelRangeRun:
    outcomes = (
        [EnumRangeSampleOutcome.PASS] * passes
        + [EnumRangeSampleOutcome.FAIL] * fails
        + [EnumRangeSampleOutcome.INCOMPLETE] * incomplete
    )
    return ModelRangeRun(
        run_id=run_id,
        sampling_seed=sampling_seed,
        temperature_forced=temperature_forced,
        retried_until_pass=retried_until_pass,
        samples=tuple(
            ModelRangeSample(case_id=f"{run_id}-case-{index}", outcome=outcome)
            for index, outcome in enumerate(outcomes)
        ),
    )


def _cli(tmp_path: Path, line: ModelRangeAcceptanceLine, runs: list[ModelRangeRun]):
    line_path = tmp_path / "line.yaml"
    runs_path = tmp_path / "runs.json"
    line_path.write_text(yaml.safe_dump(line.model_dump(mode="json")), encoding="utf-8")
    runs_path.write_text(
        json.dumps([run.model_dump(mode="json") for run in runs]), encoding="utf-8"
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.ranges",
            "evaluate",
            "--line",
            str(line_path),
            "--runs",
            str(runs_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


class TestThePowerAnalysis:
    def test_a_textbook_value(self) -> None:
        """One-sample proportion, one-sided: p0 0.5 vs p1 0.6, alpha 0.05,
        power 0.8 is 153 by the normal approximation (Chow, Shao and Wang,
        Sample Size Calculations in Clinical Research, section 4.1)."""
        assert (
            required_sample_size(floor=0.5, margin=0.1, confidence=0.95, power=0.8)
            == 153
        )

    def test_the_corpus_size_used_here(self) -> None:
        assert (
            required_sample_size(
                floor=_FLOOR, margin=_MARGIN, confidence=_CONFIDENCE, power=_POWER
            )
            == _N
        )

    def test_a_smaller_margin_needs_more_samples(self) -> None:
        wide = required_sample_size(floor=0.8, margin=0.1, confidence=0.95, power=0.8)
        narrow = required_sample_size(
            floor=0.8, margin=0.05, confidence=0.95, power=0.8
        )
        assert narrow > wide


class TestThePairAC2:
    """A corpus known to miss the floor is refused; one known to meet it passes."""

    def test_a_corpus_below_the_floor_is_missed_and_blocks(
        self, tmp_path: Path
    ) -> None:
        runs = [_run("run-bad", passes=62, fails=21)]  # 0.747, below 0.8
        evaluation = evaluate_range_line(_line(), runs)
        assert evaluation.verdict is EnumRangeVerdict.MISSED
        assert evaluation.lower_bound is not None
        assert evaluation.lower_bound < _FLOOR

        completed = _cli(tmp_path, _line(), runs)
        assert completed.returncode != 0, completed.stdout
        assert "MISSED" in completed.stdout

    def test_a_corpus_above_the_floor_is_met(self, tmp_path: Path) -> None:
        runs = [_run("run-good", passes=79, fails=4)]  # 0.952
        evaluation = evaluate_range_line(_line(), runs)
        assert evaluation.verdict is EnumRangeVerdict.MET, evaluation.reasons
        assert evaluation.lower_bound is not None
        assert evaluation.lower_bound >= _FLOOR

        completed = _cli(tmp_path, _line(), runs)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "MET" in completed.stdout

    def test_a_point_estimate_above_the_floor_is_not_enough(self) -> None:
        """0.84 > 0.8, but its lower bound at n=83 is not: luck does not pass."""
        runs = [_run("run-lucky", passes=70, fails=13)]
        evaluation = evaluate_range_line(_line(), runs)
        assert evaluation.point_estimate is not None
        assert evaluation.point_estimate > _FLOOR
        assert evaluation.verdict is EnumRangeVerdict.MISSED

    def test_the_result_is_an_interval_not_a_bare_mean(self) -> None:
        evaluation = evaluate_range_line(_line(), [_run("r", passes=79, fails=4)])
        assert evaluation.interval_low is not None
        assert evaluation.interval_high is not None
        assert evaluation.point_estimate is not None
        assert (
            evaluation.interval_low
            <= evaluation.point_estimate
            <= evaluation.interval_high
        )

    def test_the_evaluator_is_deterministic(self) -> None:
        runs = [_run("r", passes=75, fails=8)]
        assert evaluate_range_line(_line(), runs) == evaluate_range_line(_line(), runs)


class TestUndersizedCorporaAreRefusedAC3:
    def test_fewer_samples_than_the_power_analysis_requires_is_refused(
        self, tmp_path: Path
    ) -> None:
        runs = [_run("run-small", passes=40)]  # every sample passes, n=40
        evaluation = evaluate_range_line(_line(), runs)
        assert evaluation.verdict is EnumRangeVerdict.REFUSED
        assert any(f"required n={_N}" in reason for reason in evaluation.reasons)

        completed = _cli(tmp_path, _line(), runs)
        assert completed.returncode != 0
        assert f"required n={_N}" in completed.stdout

    def test_a_line_whose_n_was_not_sized_by_the_power_analysis_is_refused(
        self,
    ) -> None:
        runs = [_run("run", passes=50)]
        evaluation = evaluate_range_line(_line(n=50), runs)
        assert evaluation.verdict is EnumRangeVerdict.REFUSED
        assert any("not sized by the power analysis" in r for r in evaluation.reasons)
        assert any(f"required n={_N}" in r for r in evaluation.reasons)

    def test_an_empty_evaluation_is_refused_not_passed(self) -> None:
        evaluation = evaluate_range_line(_line(), [])
        assert evaluation.verdict is EnumRangeVerdict.REFUSED
        assert evaluation.point_estimate is None


class TestPinnedRunsAreRefusedAC4:
    def test_a_seed_pinned_run_is_refused(self, tmp_path: Path) -> None:
        runs = [_run("run-pinned", passes=83, sampling_seed=1234)]
        evaluation = evaluate_range_line(_line(), runs)
        assert evaluation.verdict is EnumRangeVerdict.REFUSED
        assert any("run-pinned" in r and "seed" in r for r in evaluation.reasons)

        completed = _cli(tmp_path, _line(), runs)
        assert completed.returncode != 0
        assert "REFUSED" in completed.stdout

    def test_a_forced_temperature_is_refused(self) -> None:
        runs = [_run("run-cold", passes=83, temperature_forced=True)]
        evaluation = evaluate_range_line(_line(), runs)
        assert evaluation.verdict is EnumRangeVerdict.REFUSED
        assert any("temperature" in r for r in evaluation.reasons)

    def test_a_retry_until_green_run_is_refused(self) -> None:
        runs = [_run("run-retried", passes=83, retried_until_pass=True)]
        evaluation = evaluate_range_line(_line(), runs)
        assert evaluation.verdict is EnumRangeVerdict.REFUSED
        assert any("retr" in r for r in evaluation.reasons)

    def test_one_pinned_run_among_clean_ones_still_refuses(self) -> None:
        runs = [
            _run("run-a", passes=60),
            _run("run-b", passes=40, sampling_seed=7),
        ]
        assert evaluate_range_line(_line(), runs).verdict is EnumRangeVerdict.REFUSED


class TestIncompleteRunsCountAsFailures:
    def test_incomplete_samples_count_against_the_rate(self) -> None:
        """79 of 83 complete-and-pass would meet; 10 incomplete among them do not
        disappear from the denominator."""
        runs = [_run("run", passes=69, fails=4, incomplete=10)]
        evaluation = evaluate_range_line(_line(), runs)
        assert evaluation.observed_n == _N
        assert evaluation.incomplete == 10
        assert evaluation.point_estimate == pytest.approx(69 / 83)

    def test_a_declared_case_missing_from_a_run_counts_as_incomplete(self) -> None:
        declared = tuple(f"case-{i}" for i in range(_N))
        present = tuple(
            ModelRangeSample(case_id=case_id, outcome=EnumRangeSampleOutcome.PASS)
            for case_id in declared[:-3]
        )
        run = ModelRangeRun(run_id="run", samples=present)
        evaluation = evaluate_range_line(_line(case_ids=declared), [run])
        assert evaluation.observed_n == _N
        assert evaluation.incomplete == 3
        assert evaluation.passes == _N - 3


class TestTheMethodIsRequired:
    @pytest.mark.parametrize(
        "missing",
        ["sample_size", "confidence", "power", "margin", "incomplete_run_treatment"],
    )
    def test_a_line_missing_a_method_part_is_not_a_range_line(
        self, missing: str
    ) -> None:
        method = _line().method.model_dump(mode="json")
        del method[missing]
        with pytest.raises(ValueError, match=missing):
            ModelRangeMethod.model_validate(method)
