# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""node_create_ticket judges a create with the omnibase_core validator in process (OMN-22850)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_create_ticket.handlers.core_ticket_guard import (
    CORE_GUARD_ID,
    CoreTicketGuard,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    guard_finding_codes,
)

_GATE = "Gate: C7"
_CRITERION = (
    "- [ ] AC1: the check works -- falsifier: uv run pytest tests/unit/test_x.py -q"
)


def _payload(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "title": "Mint the floor ticket",
        "description": f"{_GATE}\n\n## Acceptance criteria\n{_CRITERION}",
        "parentId": "OMN-22847",
        "state": "Backlog",
    }
    base.update(extra)
    return base


@pytest.mark.unit
def test_core_guard_admits_a_valid_create_without_an_installed_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setenv(
        "ONEX_TICKET_GUARD_PATH", "/nonexistent/ticket_creation_guard.py"
    )
    decision = CoreTicketGuard(lane=None).check(_payload())
    assert decision.admitted is True
    assert decision.guard_path == CORE_GUARD_ID


@pytest.mark.unit
def test_core_guard_refuses_a_project_with_the_guards_finding_shape() -> None:
    decision = CoreTicketGuard(lane=None).check(_payload(project="Sprint 12"))
    assert decision.admitted is False
    assert "project_set" in decision.finding_codes
    # The refusal keeps the guard's line shape, so the codes read back from the reason.
    assert guard_finding_codes(decision.reason) == decision.finding_codes


@pytest.mark.unit
def test_core_guard_judges_a_node_create_as_a_lane_create() -> None:
    decision = CoreTicketGuard(lane="").check(_payload())
    assert decision.admitted is False
    assert "lane_identity_missing" in decision.finding_codes


@pytest.mark.unit
def test_handler_default_guard_is_the_core_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ONEX_TICKET_GUARD_PATH", "/nonexistent/ticket_creation_guard.py"
    )
    guard = HandlerCreateTicket()._get_guard("floor-lane")
    assert isinstance(guard, CoreTicketGuard)
    assert guard.lane == "floor-lane"
