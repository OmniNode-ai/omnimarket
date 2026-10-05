# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_create_ticket moves a ticket between workflow states (OMN-20595 follow-on).

AC1: ``operation=transition`` takes ticket_id and a target state, resolves the
     state to its Linear id, and writes it through the tracker's update_issue.
AC2: the installed ticket-creation guard judges the exact update payload first;
     a refusal, or no guard found, raises and never calls the tracker.
AC3: a missing ticket_id or state is refused; dry_run writes nothing.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    EnumTicketOperation,
    HandlerCreateTicket,
    ModelCreateTicketRequest,
    ModelTicketGuardDecision,
    TicketGuardRefusedError,
    TicketGuardUnavailableError,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_create_ticket/contract.yaml"
)


class _FakeGuard:
    def __init__(self, admitted: bool, reason: str = "") -> None:
        self.admitted = admitted
        self.reason = reason
        self.inputs: list[dict[str, object]] = []

    def check(self, tool_input: dict[str, object]) -> ModelTicketGuardDecision:
        self.inputs.append(tool_input)
        return ModelTicketGuardDecision(
            admitted=self.admitted, reason=self.reason, guard_path="/fake/guard.py"
        )


class _FakeTracker:
    def __init__(self) -> None:
        self.closed = False
        self.updates: list[tuple[str, dict[str, str]]] = []

    async def update_issue(
        self, issue_id: str, updates: dict[str, str]
    ) -> SimpleNamespace:
        self.updates.append((issue_id, updates))
        return SimpleNamespace(
            identifier=issue_id,
            title="Throwaway",
            description="",
            state="Canceled",
            url=f"https://linear.app/omninode/issue/{issue_id}",
        )

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds
        self.closed = True


class _FakeStateResolver:
    def __init__(self, state_id: str = "state-uuid-canceled") -> None:
        self.state_id = state_id
        self.calls: list[tuple[str, str]] = []

    def resolve_state_id(self, ticket_id: str, state: str) -> str:
        self.calls.append((ticket_id, state))
        return self.state_id


def _request(**kw: object) -> ModelCreateTicketRequest:
    base: dict[str, object] = {
        "operation": EnumTicketOperation.TRANSITION,
        "ticket_id": "OMN-91001",
        "state": "Canceled",
    }
    base.update(kw)
    return ModelCreateTicketRequest.model_validate(base)


def test_transition_writes_the_resolved_state_id_through_update_issue() -> None:
    tracker, resolver, guard = _FakeTracker(), _FakeStateResolver(), _FakeGuard(True)
    result = HandlerCreateTicket(
        tracker=tracker, state_resolver=resolver, ticket_guard=guard
    ).handle(_request())
    assert tracker.updates == [("OMN-91001", {"stateId": "state-uuid-canceled"})]
    assert resolver.calls == [("OMN-91001", "Canceled")]
    assert result.status == "transitioned"
    assert result.operation is EnumTicketOperation.TRANSITION
    assert result.ticket_id == "OMN-91001"
    assert result.ticket_status == "Canceled"
    assert tracker.closed


def test_transition_guard_judges_the_exact_update_payload() -> None:
    guard = _FakeGuard(True)
    HandlerCreateTicket(
        tracker=_FakeTracker(), state_resolver=_FakeStateResolver(), ticket_guard=guard
    ).handle(_request())
    assert guard.inputs == [{"id": "OMN-91001", "state": "Canceled"}]


def test_transition_guard_refusal_blocks_the_write() -> None:
    tracker, resolver = _FakeTracker(), _FakeStateResolver()
    handler = HandlerCreateTicket(
        tracker=tracker,
        state_resolver=resolver,
        ticket_guard=_FakeGuard(False, "[rule_9] x"),
    )
    with pytest.raises(TicketGuardRefusedError, match="rule_9"):
        handler.handle(_request())
    assert tracker.updates == []
    assert resolver.calls == []


def test_transition_without_a_guard_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ONEX_TICKET_GUARD_PATH", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude"))
    tracker = _FakeTracker()
    handler = HandlerCreateTicket(tracker=tracker, state_resolver=_FakeStateResolver())
    with pytest.raises(TicketGuardUnavailableError):
        handler.handle(_request())
    assert tracker.updates == []


def test_transition_dry_run_runs_the_guard_and_writes_nothing() -> None:
    tracker, resolver, guard = _FakeTracker(), _FakeStateResolver(), _FakeGuard(True)
    result = HandlerCreateTicket(
        tracker=tracker, state_resolver=resolver, ticket_guard=guard
    ).handle(_request(dry_run=True))
    assert result.status == "dry_run"
    assert result.dry_run is True
    assert guard.inputs
    assert tracker.updates == []
    assert resolver.calls == []


def test_transition_empty_result_state_fails_closed() -> None:
    class _Silent(_FakeTracker):
        async def update_issue(
            self, issue_id: str, updates: dict[str, str]
        ) -> SimpleNamespace:
            del issue_id, updates
            return SimpleNamespace(identifier="OMN-91001", state="")

    handler = HandlerCreateTicket(
        tracker=_Silent(),
        state_resolver=_FakeStateResolver(),
        ticket_guard=_FakeGuard(True),
    )
    with pytest.raises(RuntimeError, match="transitioned"):
        handler.handle(_request())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"state": ""},
        {"state": "   "},
        {"ticket_id": None},
        {"ticket_id": "not a ticket"},
    ],
)
def test_transition_refuses_missing_fields(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="requires"):
        _request(**kwargs)


def test_state_is_refused_outside_transition() -> None:
    with pytest.raises(ValueError, match="only valid with operation=transition"):
        ModelCreateTicketRequest(title="t", state="Done")


def test_transition_is_declared_in_the_contract() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert "transition" in contract["inputs"]["operation"]["allowed_values"]
    assert "state" in contract["inputs"]
    module = importlib.import_module("omnimarket.nodes.node_create_ticket.__main__")
    assert module is not None
