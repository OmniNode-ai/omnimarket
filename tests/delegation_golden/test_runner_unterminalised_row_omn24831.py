# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A row that never got its terminal is named as that, not as a behaviour miss (OMN-24831).

Scheduled run 43826372809 (2026-11-16 07:48Z, job 133161076598) published all ten corpus cases and read back a ``delegation_events``
row for every one of them, but eight scored ``terminal=unknown tokens=0/0`` and
failed:

* I9: ``terminal: expected 'completed', got 'unknown'`` plus
  ``tokens: expected positive, got input=0 output=0``;
* I10: ``terminal: expected 'failed', got 'unknown'``.

``unknown`` is what ``row_terminal`` returns for a row whose ``terminal_ok`` is
still NULL after the settle window (OMN-13543). That is a statement about the
platform on the lane -- the delegation did not terminalise, or its terminal
event was not projected -- and the old text read as five different behavioural
misses (wrong terminal, no tokens, no model) that sent the reader to the
delegation ladder instead of to the lane. The failure now says so once, in the
same way a budget timeout does (OMN-18349).

The assertion is not loosened: a case that expects a terminal still FAILS on an
unterminalised row. Only the wording changes.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.delegation_golden import runner as runner_module
from tests.delegation_golden.corpus_loader import load_corpus


def _unterminalised_row() -> dict[str, Any]:
    """The shape run 43826372809 read back: a row, no outer outcome, no telemetry."""
    return {
        "correlation_id": "6ba0cce8-7acd-4c5b-b17f-3d7e8e0d4ecc",
        "timestamp": "2026-11-16T07:57:05.000000+00:00",
        "delegated_to": "cohere/north-mini-code:free",
        "model_name": "cohere/north-mini-code:free",
        "tokens_input": 0,
        "tokens_output": 0,
        "cost_usd": 0.0,
        "quality_gate_passed": None,
        "quality_gate_detail": "",
        "terminal_ok": None,
        "terminal_failure_cause": None,
    }


def _completed_row() -> dict[str, Any]:
    return {
        "correlation_id": "2b70965b-aa34-41c9-8005-c0f07d8e28a2",
        "timestamp": "2026-09-24T07:41:02.000000+00:00",
        "delegated_to": "Qwen3.8-27B",
        "model_name": "Qwen3.8-27B",
        "tokens_input": 40,
        "tokens_output": 300,
        "cost_usd": 0.0,
        "quality_gate_passed": True,
        "quality_gate_detail": "",
        "terminal_ok": True,
        "terminal_failure_cause": None,
    }


@pytest.mark.unit
class TestAnUnterminalisedRowIsNamedAsSuch:
    @pytest.mark.parametrize("case_id", ["I9", "I10"])
    def test_one_failure_names_the_missing_terminal(self, case_id: str) -> None:
        """RED: two or three misleading lines, none of them naming the cause."""
        case = load_corpus().by_id(case_id)
        failures = runner_module.evaluate_row(case, _unterminalised_row())
        assert len(failures) == 1, failures
        assert "no terminal outcome" in failures[0], failures
        assert "terminal_ok" in failures[0], failures

    def test_it_does_not_report_telemetry_misses_it_cannot_judge(self) -> None:
        """A row with no terminal has no telemetry to hold to an expectation."""
        case = load_corpus().by_id("I9")
        failures = runner_module.evaluate_row(case, _unterminalised_row())
        assert not any("tokens: expected positive" in f for f in failures), failures
        assert not any("got 'unknown'" in f for f in failures), failures

    def test_the_case_still_fails(self) -> None:
        """Not loosened: an expected terminal is still unmet on an unterminalised row."""
        for case_id in ("I9", "I10"):
            case = load_corpus().by_id(case_id)
            assert runner_module.evaluate_row(case, _unterminalised_row()), case_id

    def test_a_terminalised_row_is_still_judged_on_its_behaviour(self) -> None:
        """Positive control: a completed row is not swallowed by the new branch."""
        case = load_corpus().by_id("I9")
        assert runner_module.evaluate_row(case, _completed_row()) == []

    def test_a_wrong_terminal_is_still_a_behavioural_miss(self) -> None:
        """Positive control: a row that DID terminalise wrongly keeps its old text."""
        case = load_corpus().by_id("I10")
        failures = runner_module.evaluate_row(case, _completed_row())
        assert any("terminal: expected 'failed'" in f for f in failures), failures
        assert not any("no terminal outcome" in f for f in failures), failures
