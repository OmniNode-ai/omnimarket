# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam G.2 of track WG: delegation score rows into range samples.

The local delegation evidence row carries ``actual_score`` and
``required_bar`` (OMN-18889, plan row G2): real numbers on a scored terminal,
NULL when no score was produced, never zero. A range over response quality
judges each row as one sample:

* both present: PASS when ``actual_score >= required_bar``, else FAIL. A real
  zero score is a FAIL;
* either NULL: INCOMPLETE. The evaluator counts it as a failure in the pass
  rate and reports it by its own count; it is never scored zero (section 2b);
* a non-finite value is refused, never coerced.

A read of the store is an observation of production traffic: unpinned,
unforced, single attempt. A caller that re-ran prompts under a pinned seed must
build its :class:`ModelRangeRun` itself and say so, and the evaluator refuses it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

from omnimarket.models.ranges import (
    EnumRangeSampleOutcome,
    ModelRangeRun,
    ModelRangeSample,
)


def _finite(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name}={value!r} is not a finite number")
    return number


def _number_or_null(correlation_id: str, name: str, value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(
            f"row {correlation_id}: {name}={value!r} is not a number or NULL"
        )
    return float(value)


def sample_outcome_from_score(
    actual_score: float | None, required_bar: float | None
) -> EnumRangeSampleOutcome:
    """PASS, FAIL, or INCOMPLETE when either column is NULL."""
    if actual_score is None or required_bar is None:
        return EnumRangeSampleOutcome.INCOMPLETE
    score = _finite("actual_score", actual_score)
    bar = _finite("required_bar", required_bar)
    return EnumRangeSampleOutcome.PASS if score >= bar else EnumRangeSampleOutcome.FAIL


def range_run_from_score_rows(
    run_id: str, rows: Iterable[Mapping[str, object]]
) -> ModelRangeRun:
    """One run whose samples are the rows, keyed by ``correlation_id``."""
    samples: list[ModelRangeSample] = []
    for index, row in enumerate(rows):
        correlation_id = row.get("correlation_id")
        if not isinstance(correlation_id, str) or not correlation_id:
            raise ValueError(f"row {index} has no correlation_id")
        score = _number_or_null(correlation_id, "actual_score", row.get("actual_score"))
        bar = _number_or_null(correlation_id, "required_bar", row.get("required_bar"))
        samples.append(
            ModelRangeSample(
                case_id=correlation_id,
                outcome=sample_outcome_from_score(score, bar),
            )
        )
    if not samples:
        raise ValueError(
            f"run {run_id}: no rows; an empty read is refused, never an empty run"
        )
    return ModelRangeRun(run_id=run_id, samples=tuple(samples))


__all__ = ["range_run_from_score_rows", "sample_outcome_from_score"]
