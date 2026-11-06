# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation terminal mapping into the ledger append contract."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_work_ledger_delegation_mirror import (
    HandlerWorkLedgerDelegationMirror,
)

pytestmark = pytest.mark.unit

RUN = UUID("6dd0aa7a-a16e-497c-9e35-83a46cda6404")


@pytest.mark.parametrize("status", ["completed", "failed", "timeout"])
def test_delegation_terminal_maps_to_status(status: str) -> None:
    terminal = ModelDelegateSkillTerminalProjection.model_validate(
        {
            "correlation_id": RUN,
            "status": status,
            "task_type": "document",
            "model_name": "lab-model",
            "quality_gate_passed": status == "completed",
            "caller_lane": "test-lane",
            "emitted_at": datetime(2026, 10, 9, 0, 10, tzinfo=UTC),
        }
    )
    mapper = HandlerWorkLedgerDelegationMirror()
    request = mapper.handle(terminal)
    cells = request.rows.split(" | ")
    assert cells[:2] == ["2026-10-09T00:10:00Z", "STATUS"]
    for cell in (
        "lane=test-lane",
        "src=delegation",
        f"run={RUN}",
        "model=lab-model",
        f"outcome={status}",
        f"quality_gate_passed={str(status == 'completed').lower()}",
        f"req={request.request_id}",
    ):
        assert cell in cells
    assert request.request_id.version == 5
    assert mapper.handle(terminal) == request
    assert request.requested_by_lane == "test-lane"
    assert request.requested_at == terminal.emitted_at


def test_mapping_escapes_cells_without_copying_prompt_or_response() -> None:
    terminal = ModelDelegateSkillTerminalProjection.model_validate(
        {
            "correlation_id": RUN,
            "status": "failed",
            "task_type": "document",
            "model_name": "model | outcome=completed\nsecond row",
            "caller_lane": "lane | actor=operator",
            "prompt_text": "private prompt",
            "response": "private response",
        }
    )
    request = HandlerWorkLedgerDelegationMirror().handle(terminal)
    assert len(request.rows.splitlines()) == 1
    assert " | outcome=completed" not in request.rows
    assert " | actor=operator" not in request.rows
    assert "private" not in request.rows


def test_recorded_budget_timeout_keeps_its_reason() -> None:
    """Replay the safe terminal fields of a recorded deployed-lane receipt.

    Run ad8e84ed-daba-4f33-b804-b05748364c29 recorded a dispatch budget
    timeout. The ledger mapper retained its outcome but discarded its cause.
    """
    terminal = ModelDelegateSkillTerminalProjection.from_payload(
        {
            "correlation_id": "5a98a1bf-2725-497b-aacc-979c729619cc",
            "status": "timeout",
            "task_type": "summarization",
            "terminal_failure_cause": "timeout",
            "quality_gate_passed": False,
        }
    )
    cells = HandlerWorkLedgerDelegationMirror().handle(terminal).rows.split(" | ")
    assert "reason=timeout" in cells
    assert "reason_source=terminal_failure_cause" in cells


@pytest.mark.parametrize(
    ("fields", "reason", "source"),
    [
        ({"status": "failed"}, "unreported", "unreported"),
        ({"status": "timeout"}, "timeout", "status"),
        (
            {"status": "timeout", "error_message": "execution budget expired"},
            "timeout",
            "status",
        ),
        ({"status": "completed"}, "none", "status"),
        (
            {"status": "failed", "error_message": "provider returned HTTP 401"},
            "auth_failed",
            "failure_evidence",
        ),
        (
            {"status": "failed", "quality_gates_failed": ["required-content"]},
            "quality_gate_refused",
            "quality_gates_failed",
        ),
        (
            {
                "status": "failed",
                "terminal_failure_cause": "timeout",
                "error_message": "provider returned HTTP 401",
            },
            "timeout",
            "terminal_failure_cause",
        ),
    ],
)
def test_reason_names_its_evidence(
    fields: dict[str, object], reason: str, source: str
) -> None:
    terminal = ModelDelegateSkillTerminalProjection.from_payload(
        {"correlation_id": RUN, "task_type": "document", **fields}
    )
    cells = HandlerWorkLedgerDelegationMirror().handle(terminal).rows.split(" | ")
    assert f"reason={reason}" in cells
    assert f"reason_source={source}" in cells
    assert "provider returned" not in " | ".join(cells)


def test_completed_escalation_does_not_inherit_an_abandoned_rung_error() -> None:
    terminal = ModelDelegateSkillTerminalProjection.from_payload(
        {
            "correlation_id": RUN,
            "status": "completed",
            "task_type": "document",
            "attempts_count": 2,
            "quality_gate_passed": True,
            "attempts": [
                {
                    "tier": "local",
                    "backend_id": "local",
                    "model_id": "lab-model",
                    "quality_gate_passed": False,
                    "error_message": "provider returned HTTP 401",
                },
                {
                    "tier": "cloud",
                    "backend_id": "cloud",
                    "model_id": "cloud-model",
                    "quality_gate_passed": True,
                },
            ],
        }
    )
    cells = HandlerWorkLedgerDelegationMirror().handle(terminal).rows.split(" | ")
    assert "reason=none" in cells


def test_reason_cells_escape_delimiters_and_keep_returned_attempt_evidence() -> None:
    terminal = ModelDelegateSkillTerminalProjection.from_payload(
        {
            "correlation_id": RUN,
            "status": "failed",
            "task_type": "document",
            "terminal_failure_cause": "returned_to_caller",
            "attempts": [
                {
                    "tier": "cheap_frontier",
                    "backend_id": "cloud",
                    "model_id": "cloud-model",
                    "quality_gate_passed": False,
                    "failure_class": "rate_limited | outcome=completed\nsecond row",
                }
            ],
        }
    )
    row = HandlerWorkLedgerDelegationMirror().handle(terminal).rows
    assert "reason=returned_to_caller" in row.split(" | ")
    assert (
        "last_attempt_failure_class=rate_limited%20%7C%20outcome%3Dcompleted%0Asecond%20row"
        in row.split(" | ")
    )
    assert len(row.splitlines()) == 1
    assert " | outcome=completed" not in row
