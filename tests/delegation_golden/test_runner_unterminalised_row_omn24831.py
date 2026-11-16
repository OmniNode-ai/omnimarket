# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A row with ``terminal_ok`` NULL is named as that, not as a behaviour miss (OMN-24831).

Scheduled run 43826372809 (2026-11-16 07:48Z, job 133161076598, step "Run
Layer-2 golden-task runner (scoreboard)") published all ten corpus cases and
read back a ``delegation_events`` row for every one of them. Every row read
``tokens=0/0`` and none had a terminal: eight cases failed hard (``HARD
FAILURES: I1, I3, I5, I6, I7, I8, I9, I10``) and I2 and I4 failed as tracked
xfails. The model column was populated on eight of the ten rows
(``cohere/north-mini-code:free`` on I1 to I4 and I7 to I9, ``Qwen3.8-27B`` on
I5) and read ``none`` on I6 and I10. The pytest leg of the same job printed,
for example:

* I9: ``terminal: expected 'completed', got 'unknown'`` plus
  ``tokens: expected positive, got input=0 output=0``;
* I10: ``terminal: expected 'failed', got 'unknown'``.

``unknown`` is what ``row_terminal`` returns for a row whose ``terminal_ok`` is
NULL in the row read (OMN-13543). The old text spread that one fact over a
wrong-terminal line and a token line, which sent the reader to the delegation
ladder instead of to the row. ``evaluate_row`` now says it once. It cannot know
whether a settle window elapsed, so the text states only that ``terminal_ok``
is NULL in the row read.

The assertion is not loosened: a case that expects a terminal still FAILS on
such a row. Only the wording changes, and the gate is ``terminal_ok`` being
NULL, not the reduced terminal being ``unknown`` alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.delegation_golden import runner as runner_module
from tests.delegation_golden.corpus_loader import load_corpus

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The eight cases the nightly scored as hard failures, with the model column
# each row carried in job 133161076598 (None prints as ``none`` there).
_NIGHTLY_HARD_FAILURES: dict[str, str | None] = {
    "I1": "cohere/north-mini-code:free",
    "I3": "cohere/north-mini-code:free",
    "I5": "Qwen3.8-27B",
    "I6": None,
    "I7": "cohere/north-mini-code:free",
    "I8": "cohere/north-mini-code:free",
    "I9": "cohere/north-mini-code:free",
    "I10": None,
}


def _unterminalised_row(
    model_name: str | None = "cohere/north-mini-code:free",
) -> dict[str, Any]:
    """The shape run 43826372809 read back: a row, no outer outcome, zero telemetry."""
    return {
        "correlation_id": "6ba0cce8-7acd-4c5b-b17f-3d7e8e0d4ecc",
        "timestamp": "2026-11-16T07:57:05.000000+00:00",
        "delegated_to": model_name,
        "model_name": model_name,
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
    @pytest.mark.parametrize("case_id", sorted(_NIGHTLY_HARD_FAILURES))
    def test_one_failure_names_the_null_terminal_ok(self, case_id: str) -> None:
        """RED at the base: two or three lines, none of them naming the cause."""
        case = load_corpus().by_id(case_id)
        row = _unterminalised_row(_NIGHTLY_HARD_FAILURES[case_id])
        failures = runner_module.evaluate_row(case, row)
        assert len(failures) == 1, failures
        assert "no terminal outcome" in failures[0], failures
        assert "terminal_ok is NULL in the row read" in failures[0], failures
        assert "settle window" not in failures[0], failures

    def test_it_does_not_report_telemetry_misses_it_cannot_judge(self) -> None:
        """A row with no outer outcome has no telemetry to hold to an expectation."""
        case = load_corpus().by_id("I9")
        failures = runner_module.evaluate_row(case, _unterminalised_row())
        assert not any("tokens: expected positive" in f for f in failures), failures
        assert not any("got 'unknown'" in f for f in failures), failures

    @pytest.mark.parametrize("case_id", sorted(_NIGHTLY_HARD_FAILURES))
    def test_the_case_still_fails(self, case_id: str) -> None:
        """Not loosened: an expected terminal is still unmet on a NULL terminal_ok."""
        case = load_corpus().by_id(case_id)
        row = _unterminalised_row(_NIGHTLY_HARD_FAILURES[case_id])
        assert runner_module.evaluate_row(case, row), case_id

    def test_every_nightly_hard_failure_declares_a_terminal(self) -> None:
        """The branch only short-circuits a case that declares one; all eight do."""
        corpus = load_corpus()
        undeclared = [
            case_id
            for case_id in _NIGHTLY_HARD_FAILURES
            if corpus.by_id(case_id).expected.terminal is None
        ]
        assert undeclared == []


@pytest.mark.unit
class TestTheGateIsTerminalOkNull:
    def test_unknown_terminal_with_terminal_ok_set_is_not_named_as_null(self) -> None:
        """RED if gated on ``terminal == 'unknown'`` alone.

        An explicit ``status`` of 'unknown' reduces to 'unknown' while the row
        carries ``terminal_ok=True``. That row HAS an outer outcome, so the text
        must not claim ``terminal_ok`` is NULL; it keeps the old terminal line.
        """
        case = load_corpus().by_id("I9")
        row = _completed_row()
        row["status"] = "unknown"
        assert runner_module.row_terminal(row) == "unknown"
        failures = runner_module.evaluate_row(case, row)
        assert not any("no terminal outcome" in f for f in failures), failures
        assert not any("terminal_ok is NULL" in f for f in failures), failures
        assert any(
            "terminal: expected 'completed', got 'unknown'" in f for f in failures
        ), failures

    def test_a_case_without_a_declared_terminal_keeps_the_old_path(self) -> None:
        """``expected.terminal`` None on a NULL ``terminal_ok`` row is not named.

        U1 declares no terminal, so the ``exp.terminal is not None`` guard leaves
        it out of the new branch: it never reads "no terminal outcome".
        """
        case = load_corpus().by_id("U1")
        assert case.expected.terminal is None
        failures = runner_module.evaluate_row(case, _unterminalised_row())
        assert not any("no terminal outcome" in f for f in failures), failures


@pytest.mark.unit
class TestATerminalisedRowIsStillJudged:
    def test_a_completed_row_with_telemetry_passes(self) -> None:
        """Positive control: a completed row is not swallowed by the new branch."""
        case = load_corpus().by_id("I9")
        assert runner_module.evaluate_row(case, _completed_row()) == []

    def test_a_completed_row_with_zero_tokens_yields_the_tokens_line(self) -> None:
        """A row with ``terminal_ok`` True and zero tokens is judged on its tokens."""
        case = load_corpus().by_id("I9")
        row = _completed_row()
        row["tokens_input"] = 0
        row["tokens_output"] = 0
        failures = runner_module.evaluate_row(case, row)
        assert any(
            "tokens: expected positive, got input=0 output=0" in f for f in failures
        ), failures
        assert not any("no terminal outcome" in f for f in failures), failures

    def test_a_wrong_terminal_is_still_a_behavioural_miss(self) -> None:
        """Positive control: a row that DID terminalise wrongly keeps its old text."""
        case = load_corpus().by_id("I10")
        failures = runner_module.evaluate_row(case, _completed_row())
        assert any("terminal: expected 'failed'" in f for f in failures), failures
        assert not any("no terminal outcome" in f for f in failures), failures


@pytest.mark.unit
class TestNoConsumerClassifiesFailuresByText:
    """The consumers of the failure text are read, and none matches on it."""

    _FAILURE_TEXT = (
        "no terminal outcome",
        "got 'unknown'",
        "terminal_ok is NULL",
        "lane/platform fact",
    )

    @pytest.mark.parametrize(
        "relative",
        [
            "tests/delegation_golden/test_layer2_live_runner.py",
            ".github/workflows/delegation-regression-nightly.yml",
        ],
    )
    def test_consumer_does_not_match_on_the_failure_text(self, relative: str) -> None:
        text = (_REPO_ROOT / relative).read_text(encoding="utf-8")
        matched = [line for line in self._FAILURE_TEXT if line in text]
        assert matched == [], f"{relative} matches on failure text: {matched}"

    def test_the_verdict_is_the_failure_list_not_its_text(self) -> None:
        """A case passes iff ``evaluate_row`` returns no failure; the wording is free."""
        case = load_corpus().by_id("I9")
        assert runner_module.evaluate_row(case, _unterminalised_row()) != []
        assert runner_module.evaluate_row(case, _completed_row()) == []
