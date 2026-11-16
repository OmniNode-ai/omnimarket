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
from tests.delegation_golden.corpus_loader import ModelCorpusCase, load_corpus

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
        """RED at the base: one to four lines (I10 reads one, I1 reads four), none naming the cause."""
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
        it out of the new branch. Its exact failure list is empty: it asserts
        nothing about the row. An absence check alone would also pass if U1
        returned before the new branch, so the list is pinned, and the I9 case
        below pins a list that is NOT empty.
        """
        case = load_corpus().by_id("U1")
        assert case.expected.terminal is None
        assert runner_module.evaluate_row(case, _unterminalised_row()) == []

    def test_an_undeclared_terminal_is_judged_by_the_old_lines_exactly(self) -> None:
        """I9 with its terminal undeclared reaches the old token line, and only it.

        The row is unterminalised (terminal unknown, ``terminal_ok`` NULL), so
        only the ``exp.terminal is not None`` guard keeps it out of the new
        branch. Past that guard the old path yields exactly the token line: no
        terminal line (nothing declared), no ``no terminal outcome``. A guard
        that returned early, or dropped the ``exp.terminal`` test, changes this
        list.
        """
        base = load_corpus().by_id("I9")
        case = _declaring_terminal(base, None)
        assert runner_module.evaluate_row(case, _unterminalised_row()) == [
            "tokens: expected positive, got input=0 output=0"
        ]


def _declaring_terminal(case: ModelCorpusCase, terminal: str | None) -> ModelCorpusCase:
    """A copy of ``case`` whose expected terminal is ``terminal``, unvalidated."""
    expected = case.expected.model_copy(update={"terminal": terminal})
    return case.model_copy(update={"expected": expected})


@pytest.mark.unit
class TestACaseDeclaringUnknownIsNotShortCircuited:
    """R is ``exp.terminal != terminal``: a declared ``unknown`` is not unmet."""

    def test_the_corpus_declares_no_unknown_terminal(self) -> None:
        """The typed corpus only allows completed and failed, and none says unknown."""
        declared = {
            case.id: case.expected.terminal
            for case in load_corpus().cases
            if case.expected.terminal is not None
        }
        assert declared, "no case declares a terminal"
        assert set(declared.values()) <= {"completed", "failed"}, declared

    def test_a_declared_unknown_on_a_null_row_has_no_terminal_failure(self) -> None:
        """At the base a case declaring ``unknown`` had no terminal line on this row.

        RED without the ``exp.terminal != terminal`` leg: the branch fires and
        names ``no terminal outcome`` for a terminal the row did show.
        """
        case = _declaring_terminal(load_corpus().by_id("I9"), "unknown")
        failures = runner_module.evaluate_row(case, _unterminalised_row())
        assert not any("no terminal outcome" in f for f in failures), failures
        assert not any("terminal: expected" in f for f in failures), failures
        assert failures == ["tokens: expected positive, got input=0 output=0"]


@pytest.mark.unit
class TestXfailTrackingIsNotKeyedOnTheFailureText:
    """I2 and I4 stay tracked whatever line their failure carries."""

    @pytest.mark.parametrize("case_id", ["I2", "I4"])
    def test_the_xfail_ticket_does_not_depend_on_the_failure_wording(
        self, case_id: str
    ) -> None:
        case = load_corpus().by_id(case_id)
        assert case.xfail is not None
        null_row = runner_module._result_from_row(case, "c-null", _unterminalised_row())
        wrong = runner_module._result_from_row(case, "c-wrong", _completed_row())
        # Two different failure texts, both non-empty, one ticket for both.
        assert null_row.failures
        assert wrong.failures
        assert null_row.failures != wrong.failures
        for result in (null_row, wrong):
            assert result.passed is False
            assert result.xfail_ticket == case.xfail.ticket

    @pytest.mark.parametrize("case_id", ["I2", "I4"])
    def test_a_tracked_case_is_not_a_hard_failure_on_either_wording(
        self, case_id: str
    ) -> None:
        case = load_corpus().by_id(case_id)
        results = [
            runner_module._result_from_row(case, "c-null", _unterminalised_row()),
            runner_module._result_from_row(case, "c-wrong", _completed_row()),
        ]
        scoreboard = runner_module.Scoreboard(
            lane=runner_module._LANE,
            corpus_version="1.0.0",
            started_at="t0",
            finished_at="t1",
            results=results,
        )
        assert scoreboard.hard_failures == []


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
    """The consumers of the failure text are read, and none matches on it.

    The needles are the lines the old runner printed and the Layer-2 consumers
    could have keyed on, plus the two this change introduces. The consumers are
    the delegation-health-check caller workflow, the nightly workflow and the
    OMN-18349 placement and budget tests that read it, and the delegation health
    compute handlers. The health check itself lives in omnibase_infra and reads
    a workflow verdict, not failure lines.
    """

    _NEEDLES = (
        "terminal: expected",
        "tokens: expected positive",
        "got 'unknown'",
        "no terminal outcome",
        "terminal_ok is NULL",
    )

    _CONSUMERS = (
        ".github/workflows/delegation-health-check.yml",
        ".github/workflows/delegation-regression-nightly.yml",
        "tests/test_delegation_nightly_placement.py",
        "tests/test_delegation_nightly_budget.py",
        "src/omnimarket/nodes/node_delegation_health_compute/handlers/handler_delegation_health.py",
    )

    @classmethod
    def _matched(cls, text: str) -> list[str]:
        return [needle for needle in cls._NEEDLES if needle in text]

    @pytest.mark.parametrize("relative", _CONSUMERS)
    def test_consumer_does_not_match_on_the_failure_text(self, relative: str) -> None:
        text = (_REPO_ROOT / relative).read_text(encoding="utf-8")
        assert self._matched(text) == [], f"{relative} matches on failure text"

    @pytest.mark.parametrize(
        "needle", ["terminal: expected", "tokens: expected positive"]
    )
    def test_the_scan_flags_a_consumer_that_matches_the_old_line(
        self, needle: str
    ) -> None:
        """Positive control: a consumer carrying the old line is reported."""
        assert self._matched(f"if grep -q '{needle}' log; then retry; fi") == [needle]

    def test_the_verdict_is_the_failure_list_not_its_text(self) -> None:
        """A case passes iff ``evaluate_row`` returns no failure; the wording is free."""
        case = load_corpus().by_id("I9")
        assert runner_module.evaluate_row(case, _unterminalised_row()) != []
        assert runner_module.evaluate_row(case, _completed_row()) == []
