# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448: a failed run's row names the model, backend, host and duration.

Measured on the dev lane over the week to 2026-10-22: of 1,630 failed
``delegation_events`` rows, 983 had a blank or ``none`` model, 1,629 had no
``backend_id`` and 1,397 read a 0 ms duration, while 72 of 20,817 passed rows
lacked a backend. The row builder named the backend, host and routed model only
for an accepted rung, so a run refused on every rung named none of them, and a
terminal whose port timed no call wrote ``latency_ms`` 0.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _response_from_result,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

pytestmark = pytest.mark.unit


def _refused_rung(model: str, backend: str, host: str, tier: str) -> dict[str, object]:
    return {
        "tier": tier,
        "backend_id": backend,
        "host": host,
        "model_id": model,
        "quality_gate_passed": False,
        "quality_score": 0.0,
        "cost_usd": 0.0,
        "failure_class": "quality_gate_failed",
        "error_message": "TASK_MISMATCH: failed covers_dependencies",
        "acceptance_decision": "climb",
        "acceptance_reason": "acceptance_criteria_failed",
    }


def _failed_terminal(
    attempts: list[dict[str, object]],
    *,
    latency_ms: int = 0,
    execution_duration_ms: int | None = 110142,
) -> dict[str, object]:
    return {
        "_event_type": "onex.evt.omnimarket.delegate-skill-failed.v1",
        "tenant_id": "omninode",
        "status": "failed",
        "correlation_id": str(uuid4()),
        "timestamp": "2026-10-22T14:10:44+00:00",
        "task_type": "summarization",
        "model_name": "",
        "response": "",
        "quality_gate_passed": False,
        "quality_score": 0.0,
        "terminal_failure_cause": "quality_gate_refused",
        "error_message": "every rung was refused by the quality gate",
        "metrics": {
            "input_tokens": 933,
            "output_tokens": 174,
            "latency_ms": latency_ms,
        },
        "execution_duration_ms": execution_duration_ms,
        "attempts": attempts,
    }


def _project(event: dict[str, object]) -> dict[str, Any]:
    db = InmemoryDatabaseAdapter()
    result = HandlerProjectionDelegation().handle({**event, "_db": db})
    assert result["rows_upserted"] == 1
    rows = db.query(TABLE)
    assert len(rows) == 1
    return rows[0]


def test_a_run_refused_on_every_rung_names_its_deciding_rung() -> None:
    row = _project(
        _failed_terminal(
            [
                _refused_rung(
                    "Qwen3.8-27B", "local-heavy-reasoning", "lab-host-a", "local"
                ),
                _refused_rung(
                    "cohere/north-mini-code:free",
                    "openrouter-north-mini",
                    "openrouter.ai",
                    "cheap_frontier",
                ),
            ]
        )
    )
    assert row["terminal_ok"] is False
    assert row["terminal_failure_cause"] == "quality_gate_refused"
    assert row["model_name"] == "cohere/north-mini-code:free"
    assert row["delegated_to"] == "cohere/north-mini-code:free"
    assert row["routed_model"] == "cohere/north-mini-code:free"
    assert row["backend_id"] == "openrouter-north-mini"
    assert row["host"] == "openrouter.ai"
    assert row["cost_tier_name"] == "cheap_frontier"
    # A refused rung did not deliver the run's answer.
    assert not row.get("answering_backend")


def test_a_failure_with_no_rungs_reads_not_routed_with_its_reason() -> None:
    """OMN-19448 rule 2: a run that failed before any try never reads blank."""
    row = _project(_failed_terminal([]))
    assert row["model_name"] == "not routed (quality_gate_refused)"
    assert row["delegated_to"] == row["model_name"]
    assert row["backend_id"] == "not routed"
    assert row["host"] == "not routed"
    assert not row.get("answering_backend")


@pytest.mark.parametrize("cause", ["timeout", "runtime_shutdown"])
def test_a_run_that_failed_before_any_try_names_why_it_was_not_routed(
    cause: str,
) -> None:
    """OMN-19448 rule 2: timeouts and runtime shutdown carry no try."""
    event = {**_failed_terminal([]), "terminal_failure_cause": cause}
    row = _project(event)
    assert row["model_name"] == f"not routed ({cause})"
    assert row["backend_id"] == "not routed"


def _project_in_order(
    events: list[dict[str, object]],
) -> dict[str, Any]:
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegation()
    for event in events:
        handler.handle({**event, "_db": db})
    rows = db.query(TABLE)
    assert len(rows) == 1
    return rows[0]


@pytest.mark.parametrize("routed_first", [True, False])
def test_a_failed_run_keeps_its_model_whatever_order_events_arrive_in(
    routed_first: bool,
) -> None:
    """OMN-19448 rule 2: a sparse failed terminal never blanks a named model.

    The same run is told twice: once with its refused rung, once as a late
    terminal that carries no try (a timeout, a shutdown). Either order leaves
    the row naming the rung's model, backend and host.
    """
    correlation_id = str(uuid4())
    with_rung = {
        **_failed_terminal(
            [
                _refused_rung(
                    "Qwen3.8-27B", "local-heavy-reasoning", "lab-host-a", "local"
                )
            ]
        ),
        "correlation_id": correlation_id,
    }
    sparse = {
        **_failed_terminal([]),
        "correlation_id": correlation_id,
        "terminal_failure_cause": "timeout",
    }
    events = [with_rung, sparse] if routed_first else [sparse, with_rung]
    row = _project_in_order(events)
    assert row["terminal_ok"] is False
    assert row["model_name"] == "Qwen3.8-27B"
    assert row["delegated_to"] == "Qwen3.8-27B"
    assert row["backend_id"] == "local-heavy-reasoning"
    assert row["host"] == "lab-host-a"


def test_a_terminal_that_timed_no_call_reports_its_measured_duration() -> None:
    row = _project(_failed_terminal([], latency_ms=0, execution_duration_ms=110142))
    assert row["latency_ms"] == 110142
    assert row["delegation_latency_ms"] == 110142


def test_a_measured_latency_is_kept() -> None:
    row = _project(_failed_terminal([], latency_ms=1200, execution_duration_ms=110142))
    assert row["latency_ms"] == 1200


def _request() -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="summarize the facts below in at most 85 words",
        task_type="summarization",
        source="claude-code",
        correlation_id=uuid4(),
    )


def test_the_producer_reports_the_duration_when_the_port_names_no_latency() -> None:
    response = _response_from_result(
        _request(),
        {
            "status": "timeout",
            "content": "",
            "error_message": "timed out at stage=terminal_wait",
            "terminal_failure_cause": "timeout",
        },
        tenant_id=None,
        queue_wait_ms=None,
        execution_duration_ms=50012,
        budget_evidence=None,
    )
    assert response.metrics.latency_ms == 50012


def test_the_producer_keeps_a_latency_the_port_measured() -> None:
    response = _response_from_result(
        _request(),
        {
            "status": "completed",
            "content": "an answer",
            "quality_gate_passed": True,
            "quality_score": 1.0,
            "latency_ms": 11780,
        },
        tenant_id=None,
        queue_wait_ms=None,
        execution_duration_ms=12851,
        budget_evidence=None,
    )
    assert response.metrics.latency_ms == 11780
