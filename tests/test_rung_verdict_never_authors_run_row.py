# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448: a rung's quality verdict never authors or overrides its run's row.

Recorded on the dev lane, 2026-10-22. Run ``0c94d119`` (summarization, model
Qwen3.8-27B) climbed: its first rung was refused with ``TASK_MISMATCH: response
has 122 words, the prompt allows at most 85`` and its second rung, on the same
model, passed with an 85-word answer; the delegate-skill terminal says
``completed``. The first rung's ``quality-gate-result.v1`` reached the projection
before that terminal and created the ``delegation_events`` row with an empty task
type and model, zero tokens, a 0.0 score and ``quality_gate_passed`` false. The
local dashboard read it as "last run failed, model unknown". A one-second poll of
the decisions projection on the same lane caught the next climbing run
(``b8c30f5c``) holding that partial row for 2 min 18 s.

The terminal carries every rung on its ladder and owns the row. These tests
replay the recorded order (rung verdict, terminal, a redelivered rung verdict)
and assert the row reads the run, not the rung.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from omnimarket.models.delegation.wire.model_quality_gate import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_delegation.models.model_terminal_precedence import (
    rung_verdict_fill,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

pytestmark = pytest.mark.unit

_CORRELATION_ID = "0c94d119-e46c-460b-9f3c-8a2a3980ee73"
_MODEL = "Qwen3.8-27B"
_BACKEND = "local-heavy-reasoning"
_HOST = "lab-host-a"
_REFUSAL = "TASK_MISMATCH: response has 122 words, the prompt allows at most 85"
_RUNG_VERDICT_AT = datetime(2026, 10, 22, 14, 17, 39, tzinfo=UTC)


def _rung_one_refusal() -> ModelQualityGateResult:
    """The first rung's verdict as the quality-gate reducer published it."""
    return ModelQualityGateResult(
        correlation_id=UUID(_CORRELATION_ID),
        passed=False,
        fail_category="fail_heuristic",
        quality_score=0.0,
        failure_reasons=(_REFUSAL,),
        fallback_recommended=True,
    )


def _rung(kind: str, *, passed: bool) -> dict[str, object]:
    del kind  # names the rung for the reader; the wire record declares no kind
    return {
        # No attempt_kind: the typed lineage (attempt and parent ids) is not what
        # this test is about, and a declared kind requires it.
        "tier": "local",
        "backend_id": _BACKEND,
        "host": _HOST,
        "model_id": _MODEL,
        "quality_gate_passed": passed,
        "quality_score": 1.0 if passed else 0.0,
        "cost_usd": 0.0,
        "failure_class": None if passed else "quality_gate_failed",
        "error_message": "" if passed else _REFUSAL,
        "acceptance_decision": "accept" if passed else "climb",
        "acceptance_reason": (
            "quality_bar_met" if passed else "acceptance_criteria_failed"
        ),
    }


def _completed_terminal() -> dict[str, object]:
    """The run's delegate-skill-completed terminal, fields as recorded."""
    return {
        "_event_type": "onex.evt.omnimarket.delegate-skill-completed.v1",
        "tenant_id": "omninode",
        "status": "completed",
        "correlation_id": _CORRELATION_ID,
        "timestamp": "2026-10-22T14:17:45.994874+00:00",
        "task_type": "summarization",
        "model_name": _MODEL,
        "provider": "local",
        "response": "an 85-word report",
        "quality_gate_passed": True,
        "quality_score": 1.0,
        "metrics": {
            "input_tokens": 1012,
            "output_tokens": 226,
            "latency_ms": 11780,
            "total_tokens": 1238,
            "tokens_to_compliance": 1238,
            "compliance_attempts": 2,
        },
        "queue_wait_ms": 255,
        "execution_duration_ms": 12851,
        "attempts": [
            _rung("first_try", passed=False),
            _rung("whole_escalation", passed=True),
        ],
    }


def _write_verdict(handler: HandlerProjectionDelegation, db: Any) -> int:
    return handler.project_quality_gate_result(
        _rung_one_refusal(),
        db,
        tenant_identity="omninode",
        event_timestamp=_RUNG_VERDICT_AT,
    ).rows_upserted


def _write_terminal(handler: HandlerProjectionDelegation, db: Any) -> int:
    return int(handler.handle({**_completed_terminal(), "_db": db})["rows_upserted"])


def test_a_rung_verdict_before_its_terminal_creates_no_row() -> None:
    db = InmemoryDatabaseAdapter()
    assert _write_verdict(HandlerProjectionDelegation(), db) == 0
    assert db.query(TABLE) == []


@pytest.mark.parametrize("redelivered", [False, True])
def test_the_recorded_order_reads_the_run_not_the_rung(redelivered: bool) -> None:
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegation()
    _write_verdict(handler, db)
    assert _write_terminal(handler, db) == 1
    if redelivered:
        # A late copy of the first rung's refusal, after the terminal.
        assert _write_verdict(handler, db) == 0
    rows = db.query(TABLE)
    assert len(rows) == 1
    row = rows[0]
    assert row["terminal_ok"] is True
    assert row["quality_gate_passed"] is True
    assert _REFUSAL not in str(row.get("quality_gate_detail") or "")
    assert row["model_name"] == _MODEL
    assert row["task_type"] == "summarization"
    assert row["backend_id"] == _BACKEND
    assert row["host"] == _HOST
    assert row["tokens_input"] == 1012
    assert row["tokens_output"] == 226
    assert row["cost_tier_name"] == "local"


def test_a_verdict_fills_only_a_blank_score_source() -> None:
    verdict = {"score_source": "deterministic_acceptance"}
    assert rung_verdict_fill(None, verdict) == {}
    assert rung_verdict_fill({}, verdict) == {}
    stored = {"correlation_id": _CORRELATION_ID, "quality_gate_passed": True}
    assert rung_verdict_fill(stored, verdict) == verdict
    assert rung_verdict_fill({**stored, "score_source": "judge"}, verdict) == {}
    assert rung_verdict_fill(stored, {"score_source": None}) == {}
    # The run's verdict, detail and score are never a verdict's to write.
    for column in ("quality_gate_passed", "quality_gate_detail", "actual_score"):
        assert column not in rung_verdict_fill(stored, {column: False, **verdict})
