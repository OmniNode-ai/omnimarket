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
