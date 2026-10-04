# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19559: terminal evidence and outcomes agree in either delivery order."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from omnibase_core.enums.enum_delegation_content_verdict import (
    EnumDelegationContentVerdict,
)
from omnibase_core.enums.enum_delegation_operational_outcome import (
    EnumDelegationOperationalOutcome,
)
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

CORRELATION_ID = "c4f2a1de-0000-4d15-9559-000000000001"
MODEL_NAME = "Qwen3-Coder-30B-A3B"
FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "seams"
    / "quota_terminal"
    / "forced_429_terminal.json"
)


def _outer_terminal(cause: str | None = None) -> dict[str, object]:
    """Use the outer wire shape from the forced-429 seam fixture."""
    payload: dict[str, object] = {
        "_event_type": (
            "onex.evt.omnimarket.delegate-skill-failed.v1"
            if cause
            else "onex.evt.omnimarket.delegate-skill-completed.v1"
        ),
        "status": "timeout"
        if cause == "timeout"
        else "failed"
        if cause
        else "completed",
        "correlation_id": CORRELATION_ID,
        "task_type": "test",
        "model_name": "" if cause else MODEL_NAME,
        "response": "" if cause else "projection proof",
        "quality_gate_passed": cause is None,
        "quality_score": 0.0 if cause else 1.0,
        "metrics": {"input_tokens": 0, "output_tokens": 0, "latency_ms": 1200},
        "attempts": (
            []
            if cause
            else [
                {
                    "tier": "local",
                    "backend_id": "local",
                    "model_id": MODEL_NAME,
                    "quality_gate_passed": True,
                    "cost_usd": 0.0,
                    "failure_class": None,
                }
            ]
        ),
    }
    if cause:
        payload["terminal_failure_cause"] = cause
    return payload


def _inner_completed() -> dict[str, object]:
    """Minimal canonical shape from test_golden_chain_projection_delegation."""
    return {
        "_event_type": "onex.evt.omnimarket.delegation-completed.v1",
        "correlation_id": CORRELATION_ID,
        "task_type": "test",
        "model_used": MODEL_NAME,
        "content": "projection proof",
        "quality_passed": True,
        "quality_score": 0.98,
        "latency_ms": 1200,
        "prompt_tokens": 144,
        "completion_tokens": 593,
        "total_tokens": 737,
        "fallback_to_claude": False,
        "operational_outcome": "completed",
        "content_verdict": "usable",
    }


def _replay(*events: dict[str, object]) -> dict[str, object]:
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegation()
    for event in events:
        result = handler.handle({**event, "_db": db})
        assert result["rows_upserted"] == 1
    rows = db.query(TABLE)
    assert len(rows) == 1
    return rows[0]


def _assert_success(row: dict[str, object]) -> None:
    assert row["terminal_ok"] is True
    assert row["model_name"] == MODEL_NAME
    assert row["quality_gate_passed"] is True
    assert row["terminal_failure_cause"] is None
    assert row["attempt_history"]


def _assert_consistent_failure(
    row: dict[str, object], cause: str, outcome: str
) -> None:
    assert row["terminal_ok"] is False
    assert row["terminal_failure_cause"] == cause
    assert row["operational_outcome"] == outcome
    assert row["content_verdict"] == "not_applicable"


@pytest.mark.unit
def test_late_handler_timeout_keeps_evidenced_success() -> None:
    _assert_success(_replay(_outer_terminal(), _outer_terminal("timeout")))


@pytest.mark.unit
def test_success_after_handler_timeout_wins() -> None:
    row = _replay(_outer_terminal("timeout"), _outer_terminal())
    _assert_success(row)
    assert row["operational_outcome"] == "completed"
    assert row["content_verdict"] == "usable"


@pytest.mark.unit
def test_quota_failure_stays_sticky_after_success() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    row = _replay(*fixture["terminal_events"])
    assert row["terminal_ok"] is False
    assert row["terminal_failure_cause"] == "provider_quota_exhausted"

    success = _outer_terminal()
    success["correlation_id"] = fixture["correlation_id"]
    row = _replay(fixture["terminal_events"][0], success)
    assert row["terminal_ok"] is False
    assert row["terminal_failure_cause"] == "provider_quota_exhausted"
    assert row["quality_gate_passed"] is False


@pytest.mark.unit
def test_inner_completed_then_handler_timeout_is_consistent() -> None:
    row = _replay(_inner_completed(), _outer_terminal("timeout"))
    _assert_consistent_failure(row, "timeout", "timeout")


@pytest.mark.unit
def test_handler_timeout_then_inner_completed_is_consistent() -> None:
    row = _replay(_outer_terminal("timeout"), _inner_completed())
    _assert_consistent_failure(row, "timeout", "timeout")


@pytest.mark.unit
def test_runtime_shutdown_maps_to_cancelled() -> None:
    row = _replay(_inner_completed(), _outer_terminal("runtime_shutdown"))
    _assert_consistent_failure(row, "runtime_shutdown", "cancelled")


@pytest.mark.unit
def test_provider_error_maps_to_inference_failed() -> None:
    row = _replay(_inner_completed(), _outer_terminal("provider_error"))
    _assert_consistent_failure(row, "provider_error", "inference_failed")


@pytest.mark.unit
@pytest.mark.parametrize("cause", list(EnumDelegationTerminalFailureCause))
def test_outcome_for_every_failure_cause(
    cause: EnumDelegationTerminalFailureCause,
) -> None:
    from omnimarket.nodes.node_projection_delegation.models.model_terminal_precedence import (
        outcome_for_failure_cause,
    )

    expected = {
        "timeout": ("timeout", "not_applicable"),
        "no_terminal": ("timeout", "not_applicable"),
        "runtime_shutdown": ("cancelled", "not_applicable"),
        "provider_quota_exhausted": ("provider_quota", "not_applicable"),
        "quality_gate_refused": ("quality_rejected", "unusable"),
    }.get(cause.value, ("inference_failed", "not_applicable"))
    outcome, verdict = outcome_for_failure_cause(cause.value)
    assert (outcome, verdict) == expected
    assert outcome in {member.value for member in EnumDelegationOperationalOutcome}
    assert verdict in {member.value for member in EnumDelegationContentVerdict}


@pytest.mark.unit
def test_attempt_history_as_json_string() -> None:
    from omnimarket.nodes.node_projection_delegation.models.model_terminal_precedence import (
        is_evidenced_success,
    )

    row: dict[str, object] = {
        "terminal_ok": True,
        "model_name": MODEL_NAME,
        "attempt_history": json.dumps(
            [{"quality_gate_passed": True, "failure_class": None}]
        ),
    }
    assert is_evidenced_success(row)


@pytest.mark.unit
@pytest.mark.parametrize("history", [None, [], "[]", "invalid JSON", "{}", "null"])
def test_empty_or_invalid_history_is_not_success_evidence(history: object) -> None:
    from omnimarket.nodes.node_projection_delegation.models.model_terminal_precedence import (
        is_evidenced_success,
        is_unevidenced_handler_failure,
    )

    assert not is_evidenced_success(
        {"terminal_ok": True, "model_name": MODEL_NAME, "attempt_history": history}
    )
    assert is_unevidenced_handler_failure(
        {
            "terminal_failure_cause": "timeout",
            "model_name": " ",
            "attempt_history": history,
        }
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "changes",
    [
        {"terminal_ok": False},
        {"model_name": " "},
        {
            "attempt_history": [
                {"quality_gate_passed": True, "failure_class": "provider_error"}
            ]
        },
        {"attempt_history": [{"quality_gate_passed": False}]},
    ],
)
def test_success_requires_accepted_attempt_and_model(
    changes: dict[str, object],
) -> None:
    from omnimarket.nodes.node_projection_delegation.models.model_terminal_precedence import (
        is_evidenced_success,
    )

    row: dict[str, object] = {
        "terminal_ok": True,
        "model_name": MODEL_NAME,
        "attempt_history": [{"quality_gate_passed": True, "failure_class": " "}],
    }
    assert is_evidenced_success(row)
    row.update(changes)
    assert not is_evidenced_success(row)


@pytest.mark.unit
def test_precedence_preserves_all_outcome_columns_on_late_handler_failure() -> None:
    from omnimarket.nodes.node_projection_delegation.models.model_terminal_precedence import (
        apply_terminal_precedence,
        supersedes_handler_failure,
    )

    success: dict[str, object] = {
        "terminal_ok": True,
        "model_name": MODEL_NAME,
        "attempt_history": [{"quality_gate_passed": True}],
    }
    failure: dict[str, object] = {
        "correlation_id": CORRELATION_ID,
        "terminal_ok": False,
        "terminal_failure_cause": "timeout",
        "model_name": "",
        "attempt_history": [],
        **dict.fromkeys(
            (
                "quality_gate_passed",
                "delegated_to",
                "operational_outcome",
                "content_verdict",
                "escalation_count",
                "quality_gates_checked",
                "quality_gates_failed",
                "quality_gates_checked_jsonb",
                "quality_gates_failed_jsonb",
                "quality_gate_detail",
                "response_text",
            ),
            None,
        ),
    }
    assert supersedes_handler_failure(failure, success)
    apply_terminal_precedence(success, failure)
    assert failure == {"correlation_id": CORRELATION_ID}


@pytest.mark.unit
@pytest.mark.parametrize("verdict", ["usable", "correct", "unusable", None])
def test_consistency_uses_effective_values_and_preserves_other_verdicts(
    verdict: str | None,
) -> None:
    from omnimarket.nodes.node_projection_delegation.models.model_terminal_precedence import (
        apply_terminal_precedence,
    )

    existing: dict[str, object] = {
        "terminal_ok": False,
        "terminal_failure_cause": "unknown_cause",
        "operational_outcome": "completed",
        "content_verdict": verdict,
    }
    row: dict[str, object] = {}
    apply_terminal_precedence(existing, row)
    assert row["operational_outcome"] == "inference_failed"
    if verdict in {"usable", "correct"}:
        assert row["content_verdict"] == "not_applicable"
    else:
        assert "content_verdict" not in row


@pytest.mark.unit
def test_precedence_leaves_success_with_noncompleted_outcome_unchanged() -> None:
    from omnimarket.nodes.node_projection_delegation.models.model_terminal_precedence import (
        apply_terminal_precedence,
    )

    row: dict[str, object] = {"terminal_ok": True, "operational_outcome": "cancelled"}
    expected = dict(row)
    apply_terminal_precedence({}, row)
    assert row == expected


@pytest.mark.unit
def test_sqlite_integer_flags_are_read_as_booleans() -> None:
    """SQLite returns 1/0 for a boolean column; the precedence rules read them."""
    from omnimarket.nodes.node_projection_delegation.models.model_terminal_precedence import (
        apply_terminal_precedence,
        is_evidenced_success,
    )

    stored_success = {
        "terminal_ok": 1,
        "model_name": MODEL_NAME,
        "attempt_history": json.dumps(
            [{"quality_gate_passed": True, "failure_class": None}]
        ),
    }
    assert is_evidenced_success(stored_success)
    late_failure: dict[str, object] = {
        "terminal_ok": False,
        "terminal_failure_cause": "timeout",
        "model_name": "",
        "attempt_history": [],
    }
    apply_terminal_precedence(stored_success, late_failure)
    assert "terminal_ok" not in late_failure

    stored_failure = {
        "terminal_ok": 0,
        "terminal_failure_cause": "timeout",
        "operational_outcome": "completed",
        "content_verdict": "usable",
    }
    row: dict[str, object] = {}
    apply_terminal_precedence(stored_failure, row)
    assert row == {
        "operational_outcome": "timeout",
        "content_verdict": "not_applicable",
    }
