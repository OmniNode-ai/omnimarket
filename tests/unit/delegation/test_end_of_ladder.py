# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23165: when no model in the ladder answers, the terminal names why in one word.

The closed list is ``EnumDelegationEndOfLadderReason``. Each fixture below is a
frozen dispatch-port result; no provider is contacted. The CLI half drives
omnibase_infra's own renderer with an envelope whose exit code is the one a
failed delegation exits with.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import cast

import pytest
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)
from omnibase_infra.cli.delegate_human_output import render_delegate_outcome

from omnimarket.enums.enum_delegation_end_of_ladder_reason import (
    EnumDelegationEndOfLadderReason,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
    ProtocolDelegationDispatchPort,
)

pytestmark = pytest.mark.unit


def _attempt(backend: str, **overrides: object) -> dict[str, object]:
    attempt: dict[str, object] = {
        "tier": "byok",
        "backend_id": backend,
        "model_id": f"model-{backend}",
        "quality_gate_passed": False,
        "cost_usd": 0.0001,
        "failure_class": "provider_error",
        "acceptance_decision": "terminate",
        "acceptance_reason": "provider_call_failed",
        "error_message": f"{backend} failed",
    }
    attempt.update(overrides)
    return attempt


def _result(
    attempts: list[dict[str, object]], **overrides: object
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "failed",
        "quality_gate_passed": False,
        "content": "",
        "error_message": "every rung failed",
        "delegated_to": "openai",
        "model_name": "gpt-5-nano",
        "escalation_count": max(len(attempts) - 1, 0),
        "model_cloud_baseline": "claude-sonnet-5-5",
        "attempts": attempts,
    }
    result.update(overrides)
    return result


_EVERY_RUNG_FAILED = _result([_attempt("a"), _attempt("b")])
_EVERY_RUNG_RATE_LIMITED = _result(
    [
        _attempt("a", failure_class="rate_limited", http_status=429),
        _attempt("b", failure_class="rate_limited", http_status=429),
    ]
)
_NO_RUNG_REACHABLE = _result(
    [
        _attempt("a", failure_class="model_unavailable"),
        _attempt("b", failure_class="model_unavailable"),
    ]
)
_ESCALATION_LIMIT = _result(
    [
        _attempt(
            "a",
            failure_class="quality_gate_failed",
            acceptance_decision="climb",
            acceptance_reason="score_below_required_bar",
        ),
        _attempt(
            "b",
            failure_class="provider_error",
        ),
    ],
    terminal_failure_reason="max_escalation_attempts_reached",
)
_QUALITY_VETO = _result(
    [
        _attempt(
            "a",
            failure_class="quality_gate_failed",
            acceptance_decision="climb",
            acceptance_reason="heuristic_veto",
        ),
        _attempt("b", failure_class="rate_limited", http_status=429),
    ]
)

_FIXTURES: dict[EnumDelegationEndOfLadderReason, dict[str, object]] = {
    EnumDelegationEndOfLadderReason.EVERY_RUNG_FAILED: _EVERY_RUNG_FAILED,
    EnumDelegationEndOfLadderReason.EVERY_RUNG_RATE_LIMITED: _EVERY_RUNG_RATE_LIMITED,
    EnumDelegationEndOfLadderReason.NO_RUNG_REACHABLE: _NO_RUNG_REACHABLE,
    EnumDelegationEndOfLadderReason.ESCALATION_LIMIT_REACHED: _ESCALATION_LIMIT,
    EnumDelegationEndOfLadderReason.QUALITY_VETO: _QUALITY_VETO,
}

#: Zero provider calls: the run found no rung to call. The port states zero.
_ZERO_ATTEMPT_NO_RUNG_REACHABLE = _result(
    [],
    attempts_count=0,
    terminal_failure_reason="no_rung_reachable",
    error_message="no rung in the ladder could be called",
)


class _FrozenDispatchPort:
    def __init__(self, result: dict[str, object]) -> None:
        self._result = result

    async def dispatch(self, **_kwargs: object) -> dict[str, object]:
        return json.loads(json.dumps(self._result))


def _terminal(result: dict[str, object]) -> ModelDelegateSkillResponse:
    port = cast(ProtocolDelegationDispatchPort, _FrozenDispatchPort(result))
    request = ModelDelegateSkillRequest(
        prompt="Write add(a, b).",
        task_type="code_generation",
        source="external-client",
        correlation_id=uuid.uuid4(),
    )
    return asyncio.run(HandlerDelegateSkill(dispatch_port=port).handle(request))


def _exit_one(terminal: ModelDelegateSkillResponse, tmp_path: Path) -> None:
    envelope: dict[str, object] = {
        "run_id": str(uuid.uuid4()),
        "correlation_id": str(terminal.correlation_id),
        "status": "failed",
        "exit_code": 1,
        "result_model": "omnimarket.ModelDelegateSkillResponse",
        "result": terminal.model_dump(mode="json"),
    }
    outcome = render_delegate_outcome(envelope, state_root=tmp_path)
    assert outcome is not None
    assert outcome.succeeded is False
    assert outcome.stdout == ""
    assert "returned_to_caller" in outcome.stderr[0]


def test_the_closed_list_is_the_five_reasons() -> None:
    assert {reason.value for reason in EnumDelegationEndOfLadderReason} == {
        "every_rung_failed",
        "every_rung_rate_limited",
        "no_rung_reachable",
        "escalation_limit_reached",
        "quality_veto",
    }


@pytest.mark.parametrize(
    "reason", list(_FIXTURES), ids=[reason.value for reason in _FIXTURES]
)
def test_each_reason_ends_the_ladder_returned_to_caller_with_exit_1(
    reason: EnumDelegationEndOfLadderReason, tmp_path: Path
) -> None:
    """AC1: one fixture per reason gives that reason, the cause and exit 1."""
    terminal = _terminal(_FIXTURES[reason])

    assert terminal.status == "failed"
    assert (
        terminal.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.RETURNED_TO_CALLER
    )
    assert terminal.terminal_failure_reason == reason.value
    assert f"reason: {reason.value}" in terminal.error_message
    _exit_one(terminal, tmp_path)


def test_a_reason_outside_the_closed_list_never_reaches_the_terminal() -> None:
    """The last rung's own cause is an open set; the terminal's reason is not."""
    closed = {reason.value for reason in EnumDelegationEndOfLadderReason}
    for fixture in _FIXTURES.values():
        assert _terminal(fixture).terminal_failure_reason in closed


def test_zero_attempt_no_rung_reachable_ends_returned_to_caller(
    tmp_path: Path,
) -> None:
    """AC2: no provider call, no_rung_reachable, and not refused_before_dispatch."""
    terminal = _terminal(_ZERO_ATTEMPT_NO_RUNG_REACHABLE)

    assert terminal.attempts_count == 0
    assert terminal.attempts == []
    assert (
        terminal.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.RETURNED_TO_CALLER
    )
    assert (
        terminal.terminal_failure_cause
        is not EnumDelegationTerminalFailureCause.REFUSED_BEFORE_DISPATCH
    )
    assert terminal.terminal_failure_reason == "no_rung_reachable"
    assert terminal.response == ""
    _exit_one(terminal, tmp_path)


def test_zero_attempt_refusal_before_dispatch_keeps_its_own_cause() -> None:
    """Positive control: an ingress refusal is still refused_before_dispatch."""
    refused = _result(
        [],
        attempts_count=0,
        operational_outcome="boundary_failure",
        error_message="request refused at the boundary",
    )

    terminal = _terminal(refused)

    assert (
        terminal.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.REFUSED_BEFORE_DISPATCH
    )


def test_zero_attempt_count_with_any_other_cause_is_still_refused() -> None:
    """The zero-count validator admits only the two named events."""
    with pytest.raises(ValueError, match="attempts_count"):
        ModelDelegateSkillResponse.model_validate(
            {
                **_terminal(_ZERO_ATTEMPT_NO_RUNG_REACHABLE).model_dump(mode="json"),
                "terminal_failure_reason": "every_rung_failed",
            }
        )
