# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25667: a Done write runs dod_verify to a verdict, and the verdict decides.

Recorded failure (dev runtime, 2026-11-18): the ticket node refused every Done
write with ``dod_verify reached no output for OMN-24221 (exit_code=1)`` and a
cause that ended at ``Traceback (most recent call last):``. The verifier's own
stderr is the fixture below; the node cut it at 300 characters, before the
exception that names the cause.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.ticket.model_done_write_decision import (
    ModelDoneWriteDecision,
)
from omnibase_infra.handlers.done_write_receipt_guard import (
    DoneWriteReceiptGuard,
    DoneWriteRefusedError,
)

from omnimarket.models.ticket_write_io import ModelTicketWriteRequest
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    ModelCreateTicketResult,
)
from omnimarket.nodes.node_linear_ticket_write_effect.handlers.handler_linear_ticket_write import (
    _CAUSE_LIMIT,
    HandlerLinearTicketWriteEffect,
)

pytestmark = pytest.mark.unit

_RECORDED_STDERR = (
    (
        Path(__file__).parent
        / "fixtures"
        / "dod_verify_stderr_state_root_permission_denied.txt"
    )
    .read_text(encoding="utf-8")
    .strip()
)
_STATE_MODEL = (
    "omnimarket.nodes.node_dod_verify.models.model_dod_verify_state.ModelDodVerifyState"
)
_DESCRIPTION = "## Acceptance Criteria\n- **AC1**: the node refuses an unbound Done\n"


def _done_request() -> ModelTicketWriteRequest:
    return ModelTicketWriteRequest.model_validate(
        {
            "operation": "transition",
            "ticket_id": "OMN-24221",
            "state": "Done",
            "correlation_id": str(uuid4()),
            "requested_by_lane": "lane-a",
            "requested_by_run": "run-a",
            "idempotency_key": "done-a",
        }
    )


class _Core:
    """The ticket handler's Done path: the real guard, then the write."""

    def __init__(self, guard: DoneWriteReceiptGuard) -> None:
        self._guard = guard
        self.wrote = False

    def handle(self, command: object) -> ModelCreateTicketResult:
        asyncio.run(
            self._guard.enforce(ticket_id="OMN-24221", description=_DESCRIPTION)
        )
        self.wrote = True
        return ModelCreateTicketResult(
            operation="transition", status="transitioned", ticket_id="OMN-24221"
        )


def _verdict(*, status: str, bind: bool) -> dict[str, object]:
    ok = status == "verified"
    return {
        "status": status,
        "total_checks": 1,
        "verified_count": 1 if ok else 0,
        "failed_count": 0 if ok else 1,
        "non_probative_count": 0,
        "checks": [
            {
                "evidence_id": "t1",
                "status": "verified" if ok else "failed",
                "proof_class": "behavior",
                "binds_ac": ["AC1"] if bind else [],
            }
        ],
    }


def _guard_returning(
    verdict: dict[str, object], exit_code: int
) -> DoneWriteReceiptGuard:
    async def run(
        ticket_id: str, cwd: str, timeout: float
    ) -> tuple[dict[str, object] | None, int, str]:
        return {"result_model": _STATE_MODEL, "result": verdict}, exit_code, ""

    return DoneWriteReceiptGuard(run_dod_verify=run)


async def test_done_write_refusal_carries_the_dod_verify_invocation_traceback_in_full():
    """The refusal built from the recorded verifier stderr reaches the terminal whole."""
    reason = (
        f"dod_verify reached no output for OMN-24221 (exit_code=1): {_RECORDED_STDERR}"
    )
    error = DoneWriteRefusedError(
        "OMN-24221", ModelDoneWriteDecision(allowed=False, reason=reason)
    )

    class _Refusing:
        def handle(self, command: object) -> ModelCreateTicketResult:
            raise error

    handler = HandlerLinearTicketWriteEffect(handler_factory=lambda _: _Refusing())
    result = await handler.handle(_done_request())

    assert result.outcome == "failed"
    assert result.reason == "done_write_refused"
    assert result.finding_codes == ["done_write_gate_refused"]
    assert result.terminal_failure_cause == reason
    assert result.terminal_failure_cause.endswith(
        "PermissionError: [Errno 13] Permission denied: '.onex_state'"
    )


async def test_done_write_refusal_cause_stays_within_its_stated_bound():
    reason = "dod_verify reached no output for OMN-24221 (exit_code=1): " + "y" * 9000
    error = DoneWriteRefusedError(
        "OMN-24221", ModelDoneWriteDecision(allowed=False, reason=reason)
    )

    class _Refusing:
        def handle(self, command: object) -> ModelCreateTicketResult:
            raise error

    handler = HandlerLinearTicketWriteEffect(handler_factory=lambda _: _Refusing())
    result = await handler.handle(_done_request())
    assert len(result.terminal_failure_cause) == _CAUSE_LIMIT


async def test_done_write_refused_with_the_verifiers_own_verdict_text():
    """A failing verdict refuses the write; the cause is the verdict's reason, not a crash."""
    core = _Core(_guard_returning(_verdict(status="failed", bind=True), exit_code=1))
    handler = HandlerLinearTicketWriteEffect(handler_factory=lambda _: core)
    result = await handler.handle(_done_request())

    assert result.outcome == "failed"
    assert result.reason == "done_write_refused"
    assert result.finding_codes == ["done_write_gate_refused"]
    assert core.wrote is False
    assert "Traceback" not in result.terminal_failure_cause
    assert "dod_verify reached no output" not in result.terminal_failure_cause
    assert "failed" in result.terminal_failure_cause


async def test_done_write_refused_when_a_passing_verdict_binds_no_criterion_verdict_text():
    core = _Core(_guard_returning(_verdict(status="verified", bind=False), exit_code=0))
    handler = HandlerLinearTicketWriteEffect(handler_factory=lambda _: core)
    result = await handler.handle(_done_request())

    assert result.reason == "done_write_refused"
    assert core.wrote is False
    assert "dod_verify reached no output" not in result.terminal_failure_cause
    assert "AC1" in result.terminal_failure_cause


async def test_a_passing_verdict_that_binds_every_criterion_allows_done():
    core = _Core(_guard_returning(_verdict(status="verified", bind=True), exit_code=0))
    handler = HandlerLinearTicketWriteEffect(handler_factory=lambda _: core)
    result = await handler.handle(_done_request())

    assert result.outcome == "completed"
    assert core.wrote is True
