# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam G.2 of track WG: delegation score rows into the range evaluator.

OMN-18889's score half (plan row G2) persists ``actual_score`` and
``required_bar`` on the local delegation evidence row: real numbers on a scored
terminal, NULL when the delegation never produced a score, never zero. This
seam turns such rows into range samples, so the response-quality baseline (G4)
and the shadow comparison (G3) judge the same thing:

* both columns present: PASS when ``actual_score >= required_bar``, else FAIL;
* either column NULL: INCOMPLETE. It counts as a failure in the pass rate and
  is reported by its own count; it is never scored zero (section 2b);
* a non-finite score or bar is refused, never coerced.

The rows here are read back from a real SQLite table with the two columns the
G2 writer declares, so the NULL-versus-zero distinction crosses a store, not a
dict literal.
"""

from __future__ import annotations

import math
import sqlite3

import pytest

from omnimarket.models.ranges import (
    EnumIncompleteRunTreatment,
    EnumRangeSampleOutcome,
    EnumRangeVerdict,
    ModelRangeAcceptanceLine,
    ModelRangeMethod,
)
from omnimarket.ranges import (
    evaluate_range_line,
    range_run_from_score_rows,
    sample_outcome_from_score,
)

pytestmark = pytest.mark.unit


def _store(rows: list[tuple[str, float | None, float | None]]) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "create table delegation_events ("
        "correlation_id text primary key, actual_score real, required_bar real)"
    )
    connection.executemany("insert into delegation_events values (?, ?, ?)", rows)
    connection.commit()
    return connection


def _read(connection: sqlite3.Connection) -> list[dict[str, object]]:
    connection.row_factory = sqlite3.Row
    return [
        dict(row)
        for row in connection.execute(
            "select correlation_id, actual_score, required_bar "
            "from delegation_events order by correlation_id"
        )
    ]


def _line(n: int) -> ModelRangeAcceptanceLine:
    return ModelRangeAcceptanceLine(
        check_id="delegation.response_quality.seam_example",
        case_set="local delegations read from the evidence store",
        floor=0.8,
        window=f"the last {n} delegations",
        method=ModelRangeMethod(
            sample_size=n,
            confidence=0.95,
            power=0.8,
            margin=0.1,
            incomplete_run_treatment=EnumIncompleteRunTreatment.COUNT_AS_FAILURE,
        ),
    )


class TestOneRow:
    def test_a_score_at_the_bar_passes(self) -> None:
        assert sample_outcome_from_score(0.8, 0.8) is EnumRangeSampleOutcome.PASS

    def test_a_score_below_the_bar_fails(self) -> None:
        assert sample_outcome_from_score(0.79, 0.8) is EnumRangeSampleOutcome.FAIL

    def test_a_real_zero_score_is_a_fail_not_incomplete(self) -> None:
        assert sample_outcome_from_score(0.0, 0.8) is EnumRangeSampleOutcome.FAIL

    @pytest.mark.parametrize(("score", "bar"), [(None, 0.8), (1.0, None), (None, None)])
    def test_a_null_column_is_incomplete_never_zero(
        self, score: float | None, bar: float | None
    ) -> None:
        assert (
            sample_outcome_from_score(score, bar) is EnumRangeSampleOutcome.INCOMPLETE
        )

    @pytest.mark.parametrize(
        ("score", "bar"),
        [(math.nan, 0.8), (0.9, math.nan), (math.inf, 0.8), (0.9, -math.inf)],
    )
    def test_a_non_finite_value_is_refused(self, score: float, bar: float) -> None:
        with pytest.raises(ValueError, match="finite"):
            sample_outcome_from_score(score, bar)


class TestRowsFromARealStore:
    def test_null_survives_the_store_as_incomplete_and_zero_as_fail(self) -> None:
        rows = _read(
            _store(
                [
                    ("corr-a", 1.0, 0.8),
                    ("corr-b", 0.0, 0.8),
                    ("corr-c", None, None),
                ]
            )
        )
        run = range_run_from_score_rows("store-read", rows)
        outcomes = {sample.case_id: sample.outcome for sample in run.samples}
        assert outcomes == {
            "corr-a": EnumRangeSampleOutcome.PASS,
            "corr-b": EnumRangeSampleOutcome.FAIL,
            "corr-c": EnumRangeSampleOutcome.INCOMPLETE,
        }
        # A store read is an unpinned, single-attempt observation of production.
        assert run.sampling_seed is None
        assert not run.temperature_forced
        assert not run.retried_until_pass

    def test_a_row_with_no_correlation_id_is_refused(self) -> None:
        with pytest.raises(ValueError, match="correlation_id"):
            range_run_from_score_rows(
                "store-read", [{"actual_score": 1.0, "required_bar": 0.8}]
            )

    def test_an_empty_read_is_refused_not_an_empty_run(self) -> None:
        with pytest.raises(ValueError, match="no rows"):
            range_run_from_score_rows("store-read", [])

    def test_the_pair_through_the_seam(self) -> None:
        """G1's pair, fed from score rows: a corpus that misses the floor is
        refused as MISSED, one that meets it is MET."""
        n = 88
        meeting = [(f"m{i:03d}", 0.95, 0.8) for i in range(84)] + [
            (f"m{i:03d}", 0.5, 0.8) for i in range(84, n)
        ]
        missing = [(f"x{i:03d}", 0.95, 0.8) for i in range(66)] + [
            (f"x{i:03d}", None, None) for i in range(66, n)
        ]
        met = evaluate_range_line(
            _line(n), [range_run_from_score_rows("meet", _read(_store(meeting)))]
        )
        missed = evaluate_range_line(
            _line(n), [range_run_from_score_rows("miss", _read(_store(missing)))]
        )
        assert met.verdict is EnumRangeVerdict.MET, met.reasons
        assert missed.verdict is EnumRangeVerdict.MISSED, missed.reasons
        # The 22 unscored rows count against the rate and are reported by count.
        assert missed.incomplete == 22
        assert missed.failures == 22
