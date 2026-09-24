# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation the ladder failed is read as failed, not completed (OMN-13543).

Case I8 (all-tiers-exhaust) read ``terminal: expected 'failed', got 'completed'``
on scheduled run 35970840067 (stability-test lane, 2026-09-24 07:49Z,
correlation ``db03d67f``). The durable row for that correlation says the
opposite of what the scoreboard reported:

* ``terminal_ok = false``, ``quality_gate_passed = false``;
* ``attempt_history``: three local rungs (Qwen3.8-27B) and two cloud rungs
  (``cheap_cloud`` and the ``claude`` fallback, both served by
  gemini-2.5-flash), every one ``acceptance_reason=deterministic_floor_failed``,
  ``acceptance_decision=climb``, ``quality_gate_passed=false``.

The product terminalized the delegation as failed. The probe could not see it:
``row_terminal`` read ``terminal_state`` / ``status``, which
``delegation_events`` has never carried, and defaulted every row that was not a
budget timeout to ``completed``. So no case in the corpus could ever observe a
failed terminal, and every ``expected.terminal: failed`` case was red by
construction whatever the ladder did. The column that carries the outer outcome
is ``terminal_ok`` (OMN-15503, migration 0029), reduced from the attempt ladder.

Every test here was RED against the unmodified runner.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.delegation_golden import runner as runner_module
from tests.delegation_golden.corpus_loader import load_corpus

_REFUSAL = (
    "MALFORMED: response does not compile as Python: invalid syntax; "
    "TASK_MISMATCH: missing @pytest.mark.unit"
)


def _attempt(tier: str, model_id: str, cost_usd: float) -> dict[str, Any]:
    return {
        "tier": tier,
        "cost_usd": cost_usd,
        "model_id": model_id,
        "error_message": _REFUSAL,
        "quality_score": 0.433,
        "acceptance_reason": "deterministic_floor_failed",
        "acceptance_decision": "climb",
        "quality_gate_passed": False,
    }


def _i8_exhausted_row() -> dict[str, Any]:
    """The stability-test row for correlation db03d67f, run 35970840067."""
    return {
        "correlation_id": "db03d67f-14d5-4551-b9f0-ea059d6d54f1",
        "timestamp": "2026-09-24T07:49:26.554378+00:00",
        "delegated_to": "gemini-2.5-flash",
        "model_name": "gemini-2.5-flash",
        "tokens_input": 173,
        "tokens_output": 1894,
        "cost_usd": 0.00841,
        "quality_gate_passed": False,
        "quality_gate_detail": "",
        "terminal_ok": False,
        "terminal_failure_cause": None,
        "attempt_history": [
            _attempt("local", "Qwen3.8-27B", 0.0),
            _attempt("local", "Qwen3.8-27B", 0.0),
            _attempt("local", "Qwen3.8-27B", 0.0),
            _attempt("cheap_cloud", "gemini-2.5-flash", 0.004276),
            _attempt("claude", "gemini-2.5-flash", 0.004134),
        ],
    }


def _accepted_local_row() -> dict[str, Any]:
    """The same run's I1 row (correlation 2b70965b): accepted on the local rung."""
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
class TestTheProbeReadsTheOuterOutcomeTheRowCarries:
    """``terminal_ok`` is the outer outcome; the probe must read it."""

    def test_a_ladder_the_gate_refused_at_every_rung_reads_failed(self) -> None:
        """The I8 row, verbatim in its outcome fields. RED: read 'completed'."""
        assert runner_module.row_terminal(_i8_exhausted_row()) == "failed"

    def test_an_accepted_row_still_reads_completed(self) -> None:
        """Positive control: the classifier does not simply answer 'failed'."""
        assert runner_module.row_terminal(_accepted_local_row()) == "completed"

    def test_a_row_with_no_outer_outcome_is_not_called_completed(self) -> None:
        """No ``terminal_ok`` means the row does not say; the probe must not either.

        Before this change the absence of every outcome field was read as
        success, which is how a failed ladder reached the scoreboard as a
        completed one.
        """
        row = _accepted_local_row()
        del row["terminal_ok"]
        assert runner_module.row_terminal(row) == "unknown"

    def test_a_budget_timeout_still_reads_timeout_first(self) -> None:
        """A cancelled delegation also has terminal_ok=false; it stays 'timeout'."""
        row = {
            "quality_gate_passed": False,
            "terminal_ok": False,
            "quality_gate_detail": (
                "delegation exceeded the handler execution budget of 240s and "
                "was cancelled"
            ),
        }
        assert runner_module.row_terminal(row) == "timeout"


@pytest.mark.unit
class TestCaseI8IsScoredOnWhatTheLadderDid:
    """I8's own expectations, evaluated against its own lane row."""

    def test_i8_passes_on_the_row_the_lane_actually_wrote(self) -> None:
        """RED: ``terminal: expected 'failed', got 'completed'``."""
        case = load_corpus().by_id("I8")
        assert runner_module.evaluate_row(case, _i8_exhausted_row()) == []

    def test_i8_fails_if_the_ladder_had_accepted_an_answer(self) -> None:
        """The falsifier: a completed terminal on I8 is a real failure."""
        case = load_corpus().by_id("I8")
        failures = runner_module.evaluate_row(case, _accepted_local_row())
        assert any("terminal: expected 'failed'" in f for f in failures), failures
