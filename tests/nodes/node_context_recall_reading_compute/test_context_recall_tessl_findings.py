# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tessl review findings on piece 16's reading (OMN-22980); each test failed before its fix."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_context_recall_reading_compute.handlers.handler_context_recall_reading import (
    HandlerContextRecallReading,
    fact_recalled,
)
from tests.nodes.node_context_recall_reading_compute.reading_support import (
    FACT,
    eval_run,
    probe,
    request,
)

pytestmark = pytest.mark.unit


def _read(
    probes: list[dict[str, Any]] | None = None,
    eval_runs: list[dict[str, Any]] | None = None,
) -> Any:
    return HandlerContextRecallReading().handle(request(probes, eval_runs))


def test_an_answer_with_no_read_time_cannot_pass_the_order_check() -> None:
    unordered = probe()
    unordered["recall_read_at"] = None
    reading = _read([unordered])
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert "probe-out-of-order:p1" in reading.reasons


def test_an_eval_run_that_ends_before_it_starts_does_not_pass() -> None:
    reading = _read(eval_runs=[eval_run(started=900, ended=1200)])
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert "eval-ended-out-of-order:e1" in reading.reasons
    assert reading.eval_runs_with_result == 0


def test_an_eval_run_that_ends_in_the_future_does_not_pass() -> None:
    reading = _read(eval_runs=[eval_run(started=900, ended=-300)])
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert "eval-ended-out-of-order:e1" in reading.reasons


def test_a_fact_that_is_a_prefix_of_a_longer_token_is_not_recalled() -> None:
    assert not fact_recalled("heron-4471", "the codeword is heron-44710")
    assert not fact_recalled("heron-4471", "the codeword is heron-4471-b")
    assert fact_recalled("heron-4471", "The codeword is HERON-4471.")


def test_a_blank_expected_fact_is_never_recalled() -> None:
    assert not fact_recalled(" ", "any answer at all")
    blank = probe()
    blank["expected_fact"] = " "
    reading = _read([blank])
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING


def test_a_fact_inside_a_sentence_is_still_recalled() -> None:
    assert fact_recalled(FACT, f"You told me {FACT}.")


def test_below_minimum_is_reported_for_supplied_runs_when_no_probe_was_supplied() -> (
    None
):
    in_progress = eval_run(started=300, ended=None, result_id=None)
    reading = _read(probes=[], eval_runs=[in_progress])
    assert "no-probe-supplied" in reading.reasons
    assert any(r.startswith("below-minimum:eval-runs") for r in reading.reasons)
