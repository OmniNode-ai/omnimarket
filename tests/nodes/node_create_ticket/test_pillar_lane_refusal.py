# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane cannot assign a pillar owner the map keeps out of lanes' hands (OMN-17427).

The pillar map declares ``assignable_by_lanes: false`` for an owner whose tickets only the
operator or the owner files. ``node_create_ticket`` run by a lane (a declared lane, a subagent or
a headless session) then refuses ``--pillar`` for that owner, before the ticket-creation guard
spends any of the day's intake budget; an interactive operator session, a pillar that declares no
such flag, and a ticket with no pillar are admitted. Every refusal has an admitted twin beside it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.handlers.handler_lane_intake import ModelLaneIntakePolicy, lane_caller
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    ModelCreateTicketRequest,
    ModelTicketGuardDecision,
)

pytestmark = pytest.mark.unit

_LOCKED = "11111111-1111-1111-1111-111111111111"
_OPEN = "22222222-2222-2222-2222-222222222222"
_POLICY = ModelLaneIntakePolicy(label="the-label", stamp_prefix="opened-by:")
_ENV_KEYS = (
    "ONEX_LANE",
    "ONEX_REMOTE_LANE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
)


class _Guard:
    def __init__(self) -> None:
        self.inputs: list[dict[str, object]] = []

    def check(self, tool_input: dict[str, object]) -> ModelTicketGuardDecision:
        self.inputs.append(tool_input)
        return ModelTicketGuardDecision(admitted=True, guard_path="fake")


class _Client:
    def __init__(self) -> None:
        self.assignees: list[str | None] = []

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
        del title, description, team, parent, label_names
        self.assignees.append(assignee_id)
        return "OMN-90001", "https://example.invalid/OMN-90001"


@pytest.fixture
def caller_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No lane, no subagent marker and no entrypoint: the environment of nothing in particular."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _map(tmp_path: Path) -> Path:
    path = tmp_path / "pillar_owners.yaml"
    path.write_text(
        "pillars:\n"
        f"  locked:\n    owner_linear_user_id: {_LOCKED}\n    assignable_by_lanes: false\n"
        f"  open:\n    owner_linear_user_id: {_OPEN}\n"
    )
    return path


def _handler(tmp_path: Path) -> tuple[HandlerCreateTicket, _Guard, _Client]:
    guard, client = _Guard(), _Client()
    handler = HandlerCreateTicket(
        ticket_guard=guard,
        linear_client=client,
        pillar_owners_path=_map(tmp_path),
        lane_intake_policy=_POLICY,
    )
    return handler, guard, client


_LANE_ENVS = {
    "declared lane": {"ONEX_LANE": "some-lane"},
    "declared remote lane": {"ONEX_REMOTE_LANE": "some-remote-lane"},
    "subagent session": {"CLAUDE_CODE_CHILD_SESSION": "1"},
    "headless session": {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"},
}


@pytest.mark.parametrize("env", list(_LANE_ENVS), ids=list(_LANE_ENVS))
def test_a_lane_cannot_assign_the_locked_pillar_owner(
    tmp_path: Path, caller_env: pytest.MonkeyPatch, env: str
) -> None:
    for key, value in _LANE_ENVS[env].items():
        caller_env.setenv(key, value)
    handler, guard, client = _handler(tmp_path)
    with pytest.raises(RuntimeError, match="refused for a lane"):
        handler.handle(ModelCreateTicketRequest(title="t", pillar="locked"))
    assert client.assignees == []
    assert guard.inputs == [], (
        "the refusal came after the guard and spent intake budget"
    )


def test_a_lane_dry_run_is_refused_too(
    tmp_path: Path, caller_env: pytest.MonkeyPatch
) -> None:
    caller_env.setenv("ONEX_LANE", "some-lane")
    handler, guard, _ = _handler(tmp_path)
    with pytest.raises(RuntimeError, match="refused for a lane"):
        handler.handle(
            ModelCreateTicketRequest(title="t", pillar="locked", dry_run=True)
        )
    assert guard.inputs == []


def test_an_interactive_operator_session_assigns_the_locked_pillar_owner(
    tmp_path: Path, caller_env: pytest.MonkeyPatch
) -> None:
    caller_env.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    handler, _, client = _handler(tmp_path)
    handler.handle(ModelCreateTicketRequest(title="t", pillar="locked"))
    assert client.assignees == [_LOCKED]


def test_a_lane_assigns_a_pillar_whose_owner_is_not_locked(
    tmp_path: Path, caller_env: pytest.MonkeyPatch
) -> None:
    caller_env.setenv("ONEX_LANE", "some-lane")
    handler, _, client = _handler(tmp_path)
    handler.handle(ModelCreateTicketRequest(title="t", pillar="open"))
    assert client.assignees == [_OPEN]


def test_a_lane_with_no_pillar_files_under_the_creator(
    tmp_path: Path, caller_env: pytest.MonkeyPatch
) -> None:
    caller_env.setenv("ONEX_LANE", "some-lane")
    handler, _, client = _handler(tmp_path)
    handler.handle(ModelCreateTicketRequest(title="t"))
    assert client.assignees == [None]


def test_a_lane_cannot_file_into_a_project_either(
    tmp_path: Path, caller_env: pytest.MonkeyPatch
) -> None:
    """New tickets carry no project, so an owner's project is out of reach of this node."""
    caller_env.setenv("ONEX_LANE", "some-lane")
    handler, _, client = _handler(tmp_path)
    with pytest.raises(RuntimeError, match="locked"):
        handler.handle(
            ModelCreateTicketRequest(
                title="t", pillar="locked", project="Owner project"
            )
        )
    assert client.assignees == []


def test_the_packaged_map_shape_is_what_the_refusal_reads(tmp_path: Path) -> None:
    """``assignable_by_lanes: false`` is the only value that locks; a missing key or true does not."""
    path = tmp_path / "pillar_owners.yaml"
    path.write_text(
        "pillars:\n"
        f"  a:\n    owner_linear_user_id: {_OPEN}\n    assignable_by_lanes: true\n"
        f"  b:\n    owner_linear_user_id: {_OPEN}\n"
    )
    for pillar in ("a", "b"):
        handler = HandlerCreateTicket(
            ticket_guard=_Guard(),
            linear_client=_Client(),
            pillar_owners_path=path,
            lane_intake_policy=_POLICY,
        )
        handler.handle(ModelCreateTicketRequest(title="t", pillar=pillar))


@pytest.mark.parametrize(
    ("env", "lane"),
    [
        ({}, False),
        ({"CLAUDE_CODE_ENTRYPOINT": "cli"}, False),
        ({"CLAUDE_CODE_CHILD_SESSION": "0"}, False),
        ({"ONEX_LANE": "  "}, False),
        ({"ONEX_LANE": "x"}, True),
        ({"ONEX_REMOTE_LANE": "x"}, True),
        ({"CLAUDE_CODE_CHILD_SESSION": "1"}, True),
        ({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, True),
    ],
)
def test_lane_caller_reads_the_pillar_hooks_test(
    env: dict[str, str], lane: bool
) -> None:
    assert bool(lane_caller(env)) is lane
