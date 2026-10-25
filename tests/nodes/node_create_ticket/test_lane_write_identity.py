# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_create_ticket refuses a lane write that would go out under the operator (OMN-20355).

Once ``ONEX_AUTOMATION_ACTOR_ID`` is configured, a create, comment or
transition resolves the principal of its write credential first and refuses
(no Linear write) when that principal is an operator principal or is not the
declared actor. With no actor configured nothing is resolved and nothing is
refused.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from types import SimpleNamespace

import pytest

from omnimarket.handlers.handler_lane_write_identity import (
    ENV_AUTOMATION_ACTOR_ID,
    ENV_OPERATOR_PRINCIPALS,
    LaneWriteIdentityRefusedError,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    EnumTicketOperation,
    HandlerCreateTicket,
    ModelCreateTicketRequest,
    ModelTicketGuardDecision,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("lane_create_context")]

_ACTOR = "11111111-2222-4333-8444-555555555555"
_OPERATOR = "99999999-8888-4777-8666-555555555555"


class _Guard:
    def check(self, tool_input: dict[str, object]) -> ModelTicketGuardDecision:
        del tool_input
        return ModelTicketGuardDecision(admitted=True, reason="", guard_path="/g.py")


class _Linear:
    def __init__(self) -> None:
        self.created = 0

    def create_ticket(
        self,
        *,
        title: str,
        description: str,
        team: str,
        parent: str | None,
        assignee_id: str | None,
        label_names: Sequence[str] = (),
    ) -> tuple[str, str]:
        self.created += 1
        return "OMN-91001", "https://linear.app/omninode/issue/OMN-91001"


class _Tracker:
    def __init__(self) -> None:
        self.writes = 0

    async def add_comment(self, issue_id: str, body: str) -> SimpleNamespace:
        self.writes += 1
        return SimpleNamespace(id="comment-1")

    async def update_issue(
        self, issue_id: str, updates: dict[str, str]
    ) -> SimpleNamespace:
        self.writes += 1
        return SimpleNamespace(identifier=issue_id, state="Canceled", url="u")

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds


class _States:
    def resolve_state_id(self, ticket_id: str, state: str) -> str:
        return "state-id"


def _handler(
    principal: Callable[[], str], tracker: _Tracker, linear: _Linear
) -> HandlerCreateTicket:
    return HandlerCreateTicket(
        linear_client=linear,
        tracker=tracker,
        state_resolver=_States(),
        ticket_guard=_Guard(),
        principal_resolver=principal,
    )


def _requests() -> list[ModelCreateTicketRequest]:
    return [
        ModelCreateTicketRequest(title="t", description="body"),
        ModelCreateTicketRequest(
            operation=EnumTicketOperation.COMMENT, ticket_id="OMN-1", body="b"
        ),
        ModelCreateTicketRequest(
            operation=EnumTicketOperation.TRANSITION,
            ticket_id="OMN-1",
            state="Canceled",
        ),
    ]


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_AUTOMATION_ACTOR_ID, _ACTOR)
    monkeypatch.setenv(ENV_OPERATOR_PRINCIPALS, _OPERATOR)


@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize("index", [0, 1, 2], ids=["create", "comment", "transition"])
def test_write_under_the_operator_principal_is_refused_before_any_linear_write(
    index: int,
) -> None:
    tracker, linear = _Tracker(), _Linear()
    handler = _handler(lambda: _OPERATOR, tracker, linear)
    with pytest.raises(LaneWriteIdentityRefusedError, match="operator principal"):
        handler.handle(_requests()[index])
    assert tracker.writes == 0
    assert linear.created == 0


@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize("index", [0, 1, 2], ids=["create", "comment", "transition"])
def test_write_under_the_declared_actor_goes_out(index: int) -> None:
    tracker, linear = _Tracker(), _Linear()
    result = _handler(lambda: _ACTOR, tracker, linear).handle(_requests()[index])
    assert result.status in {"created", "commented", "transitioned"}


@pytest.mark.parametrize("index", [0, 1, 2], ids=["create", "comment", "transition"])
def test_without_a_configured_actor_nothing_is_resolved_or_refused(
    index: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_AUTOMATION_ACTOR_ID, raising=False)

    def _never() -> str:
        raise AssertionError("the principal must not be resolved with no actor")

    result = _handler(_never, _Tracker(), _Linear()).handle(_requests()[index])
    assert result.status in {"created", "commented", "transitioned"}


@pytest.mark.usefixtures("configured")
def test_dry_run_resolves_nothing() -> None:
    def _never() -> str:
        raise AssertionError("a dry run sends nothing and resolves nothing")

    request = ModelCreateTicketRequest(title="t", description="body", dry_run=True)
    assert _handler(_never, _Tracker(), _Linear()).handle(request).status == "dry_run"
