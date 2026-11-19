# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC8, follow-up: a failed run stored at 0 ms has no measured duration.

AC8 says a stored latency of 0 on a failed call is not a measured duration. The
duration every local page shows is ``execution_ms`` (model_run_duration.py), but
#6325 only nulled ``latency_ms``, so a failed or timed-out run whose terminal
carried ``execution_duration_ms = 0`` was still written, and served, as a measured
0 ms. Found by the independent review of 2026-11-18.

Failure modes, each with a test below:
  F1  a failed or timed-out run's 0 ms is written as a measured duration
  F2  a failed run that did time something loses its measured duration (over-nulling)
  F3  a run that ended without a failure cause loses its duration
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    _canonical_result_to_task_delegated_payload as convert,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from tests.unit.projection.test_delegation_backend_id_and_host_omn20162 import (
    _attempt,
    _project,
    _skill_terminal,
)

pytestmark = pytest.mark.unit


def _terminal(execution_ms: int, cause: str | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "correlation_id": "cid-24615-ac8",
        "task_type": "code",
        "model_used": "Qwen3.8-27B",
        "quality_passed": cause is None,
        "content": "",
        "prompt_text": "the question",
        "latency_ms": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "execution_duration_ms": execution_ms,
    }
    if cause is not None:
        payload["terminal_failure_cause"] = cause
    return payload


@pytest.mark.parametrize("cause", ["provider_error", "timeout"])
def test_failed_run_duration_zero_is_not_a_measured_duration(cause: str) -> None:
    """F1: the AC8 falsifier on the served duration column."""
    assert convert(_terminal(0, cause))["execution_ms"] is None


def test_failed_run_duration_a_measured_duration_is_kept() -> None:
    """F2."""
    assert convert(_terminal(1234, "provider_error"))["execution_ms"] == 1234


def test_failed_run_duration_a_run_without_a_failure_keeps_its_duration() -> None:
    """F3."""
    assert convert(_terminal(850, None))["execution_ms"] == 850
    assert convert(_terminal(0, None))["execution_ms"] == 0


# The review of #6442 (2026-11-18): the skill-terminal path stamps execution_ms through
# _stamp_terminal_timing_and_requested_model, not the canonical converter, so it needs the same rule.


def test_failed_run_duration_skill_terminal_at_zero_is_not_measured() -> None:
    """F4: a skill terminal whose every attempt failed and that timed nothing stores no duration."""
    payload = _skill_terminal([_attempt(backend_id="only", host="h201", passed=False)])
    payload.update(
        quality_gate_passed=False,
        execution_duration_ms=0,
        terminal_failure_cause="provider_error",
    )
    row = _project(payload, InmemoryDatabaseAdapter())
    assert row.get("terminal_failure_cause")  # the fixture is a failed run
    assert row.get("execution_ms") is None


# The re-review of #6442 (2026-11-19): the merge step lets a timing the new event leaves empty inherit the stored
# one, so a failed terminal arriving over a stored 0 ms kept it as a measured duration.


def test_failed_run_duration_a_failure_over_a_stored_zero_is_not_measured() -> None:
    """F5: a failed terminal merged over a row stored at 0 ms stores no duration."""
    db = InmemoryDatabaseAdapter()
    first = _skill_terminal([_attempt(backend_id="only", host="h201", passed=True)])
    first["execution_duration_ms"] = 0
    assert _project(first, db).get("execution_ms") == 0  # a measured 0, no failure
    failed = _skill_terminal([_attempt(backend_id="only", host="h201", passed=False)])
    failed.update(
        correlation_id=first["correlation_id"],
        quality_gate_passed=False,
        terminal_failure_cause="provider_error",
    )
    row = _project(failed, db)
    assert row.get("terminal_failure_cause")  # the merged row is a failed run
    assert row.get("execution_ms") is None


def test_failed_run_duration_a_completed_run_merged_over_a_stored_zero_keeps_it() -> (
    None
):
    """F6: the merge rule clears only a failed run's 0 ms; a run that did not fail keeps its measured 0."""
    db = InmemoryDatabaseAdapter()
    first = _skill_terminal([_attempt(backend_id="only", host="h201", passed=True)])
    first["execution_duration_ms"] = 0
    _project(first, db)
    again = _skill_terminal([_attempt(backend_id="only", host="h201", passed=True)])
    again["correlation_id"] = first["correlation_id"]
    row = _project(again, db)
    assert not row.get("terminal_failure_cause")
    assert row.get("execution_ms") == 0
