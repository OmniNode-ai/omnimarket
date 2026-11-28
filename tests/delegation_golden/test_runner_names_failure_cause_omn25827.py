# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A failed golden case names the cause the delegation row recorded (OMN-25827).

Run 45237936306 (dev nightly, job 137914560148) failed the live cases I1, I3, I5
and I7 with ``terminal: expected 'completed', got 'failed'``, ``tokens: expected
positive, got input=0 output=0`` and ``delegated_to='claude-sonnet-5-5'``. Those
three lines are all the scoreboard and the pytest output said. The cause sat in
the same ``delegation_events`` row the probe had just read: three attempts on
backend ``byok-anthropic`` at tier ``tenant_overlay``, each answered HTTP 401
"Invalid Anthropic API Key" (``failure_class`` ``provider_auth_failed``), ending
``every_rung_failed``. Four lanes read the run and none could name it, because
the probe scored the row and dropped the part that said why it failed.

The probe now appends one line to a failed case, built from the row's own
``terminal_failure_cause`` and its last attempt. It is an addition to the text of
a failure: no verdict changes, a passing row gains nothing, and the assertion
lines before it are the same ones as before.

The fixture is the recorded case output of that run, shared with the
orchestrator-side fix tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.delegation_golden import runner as runner_module
from tests.delegation_golden.corpus_loader import load_corpus

_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "unit"
    / "delegation"
    / "fixtures"
    / "omn25827_house_claude_401"
    / "recorded_case_output.json"
)


def _recorded() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_FIXTURE.read_text())
    return loaded


def _failed_row(*, attempt_history: Any) -> dict[str, Any]:
    """The projection row of a recorded failing case (I1 of run 45237936306)."""
    recorded = _recorded()
    return {
        "correlation_id": "0a0bb04b-a697-4f6e-a2e9-9c64390b937e",
        "timestamp": "2026-11-23T18:24:10.000000+00:00",
        "delegated_to": recorded["cases"]["I1"]["model"],
        "model_name": recorded["cases"]["I1"]["model"],
        "tokens_input": 0,
        "tokens_output": 0,
        "cost_usd": 0.0,
        "quality_gate_passed": False,
        "quality_gate_detail": "",
        "terminal_ok": False,
        "terminal_failure_cause": recorded["probe_terminal"]["terminal_failure_reason"],
        "attempt_history": attempt_history,
    }


@pytest.mark.unit
class TestAFailedCaseNamesItsRecordedCause:
    def test_the_recorded_case_failures_are_unchanged_and_the_cause_follows(
        self,
    ) -> None:
        recorded = _recorded()
        case = load_corpus().by_id("I1")
        row = _failed_row(attempt_history=recorded["probe_attempts"])

        failures = runner_module.evaluate_row(case, row)

        assert failures[:4] == recorded["cases"]["I1"]["failures"]
        assert len(failures) == 5
        cause = failures[4]
        assert "every_rung_failed" in cause
        assert "provider_auth_failed" in cause
        assert "byok-anthropic" in cause
        assert "tenant_overlay" in cause
        assert "HTTP 401" in cause

    def test_a_row_stored_as_a_json_string_names_the_same_cause(self) -> None:
        recorded = _recorded()
        case = load_corpus().by_id("I3")
        row = _failed_row(attempt_history=json.dumps(recorded["probe_attempts"]))

        failures = runner_module.evaluate_row(case, row)

        assert any("provider_auth_failed" in line for line in failures)

    def test_a_passing_case_gains_no_cause_line(self) -> None:
        case = load_corpus().by_id("I1")
        row = {
            "correlation_id": "2b70965b-aa34-41c9-8005-c0f07d8e28a2",
            "timestamp": "2026-11-24T07:41:02.000000+00:00",
            "delegated_to": "fixture-local-model",
            "model_name": "fixture-local-model",
            "tokens_input": 40,
            "tokens_output": 300,
            "cost_usd": 0.0,
            "quality_gate_passed": True,
            "quality_gate_detail": "",
            "terminal_ok": True,
            "terminal_failure_cause": None,
            "attempt_history": _recorded()["probe_attempts"],
        }

        assert runner_module.evaluate_row(case, row) == []

    def test_a_failed_row_with_no_recorded_cause_adds_nothing(self) -> None:
        case = load_corpus().by_id("I1")
        row = _failed_row(attempt_history=None)
        row["terminal_failure_cause"] = None

        failures = runner_module.evaluate_row(case, row)

        assert failures == _recorded()["cases"]["I1"]["failures"]

    def test_a_long_provider_body_is_bounded(self) -> None:
        case = load_corpus().by_id("I1")
        attempt = dict(_recorded()["probe_attempts"][-1])
        attempt["error_message"] = "x" * 5000
        row = _failed_row(attempt_history=[attempt])

        failures = runner_module.evaluate_row(case, row)

        assert len(failures[-1]) < 700
