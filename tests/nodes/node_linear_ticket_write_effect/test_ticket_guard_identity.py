# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Remote identity reaches the actual guarded core without process env mutation."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.handlers.handler_lane_intake import ModelLaneIntakePolicy
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    ModelCreateTicketRequest,
    ModelTicketGuardDecision,
    TicketGuardRefusedError,
)

pytestmark = pytest.mark.unit

BODY = (
    "created-by-lane: remote-a\n\n"
    "Gate: OmniNode-ai/omnimarket ci.yml\n\n"
    "## Acceptance criteria\n\n"
    "- [ ] AC1: the node refuses an invalid create -- falsifier: uv run pytest tests/nodes/node_create_ticket -q\n"
)


class Guard:
    def __init__(self, admitted: bool = True):
        self.admitted = admitted
        self.inputs = []

    def check(self, tool_input):
        self.inputs.append(tool_input)
        return ModelTicketGuardDecision(
            admitted=self.admitted,
            finding_codes=[] if self.admitted else ["missing_gate_line"],
        )


class Client:
    def __init__(self):
        self.calls = []

    def create_ticket(
        self,
        *,
        title: str,
        description: str,
        team: str,
        parent: str | None,
        assignee_id: str | None,
        label_names: Sequence[str] = (),
    ):
        self.calls.append(
            {
                "title": title,
                "description": description,
                "team": team,
                "parent": parent,
                "assignee_id": assignee_id,
                "label_names": label_names,
            }
        )
        return "OMN-99999", "https://linear.app/omninode/issue/OMN-99999"


def handler(client, guard):
    return HandlerCreateTicket(
        linear_client=client,
        ticket_guard=guard,
        lane_intake_policy=ModelLaneIntakePolicy(
            label="lane-created", stamp_prefix="created-by-lane:"
        ),
        lane_id="remote-a",
        require_description_stamp=True,
    )


def test_description_passes_through_unchanged_with_the_remote_lane(monkeypatch):
    monkeypatch.setenv("ONEX_LANE", "worker-service")
    client, guard = Client(), Guard()
    result = handler(client, guard).handle(
        ModelCreateTicketRequest(title="Scratch", description=BODY, parent="OMN-20595")
    )
    assert (
        result.description_body
        == client.calls[0]["description"]
        == guard.inputs[0]["description"]
        == BODY
    )
    assert guard.inputs[0]["state"] == "Backlog"
    assert guard.inputs[0]["parentId"] == "OMN-20595"
    assert guard.inputs[0]["labels"] == ["lane-created"]
    assert "project" not in guard.inputs[0]
    import os

    assert os.environ["ONEX_LANE"] == "worker-service"


@pytest.mark.parametrize(
    "body", ["", BODY.replace("remote-a", "someone-else"), BODY.split("\n\n", 1)[1]]
)
def test_missing_or_different_lane_stamp_never_calls_linear(body):
    client, guard = Client(), Guard()
    with pytest.raises(TicketGuardRefusedError) as exc:
        handler(client, guard).handle(
            ModelCreateTicketRequest(title="Scratch", description=body)
        )
    assert exc.value.finding_codes == ["lane_stamp_missing"]
    assert client.calls == guard.inputs == []


def test_guard_refusal_blocks_create_with_original_codes():
    client, guard = Client(), Guard(False)
    with pytest.raises(TicketGuardRefusedError) as exc:
        handler(client, guard).handle(
            ModelCreateTicketRequest(title="Scratch", description=BODY)
        )
    assert exc.value.finding_codes == ["missing_gate_line"]
    assert client.calls == []


def test_guard_is_called_with_explicit_lane_and_dry_run_does_not_spend_budget(
    monkeypatch,
):
    core = handler(Client(), Guard())
    observed = []

    def get_guard(lane="", record=True):
        observed.append((lane, record))
        return Guard()

    monkeypatch.setattr(core, "_get_guard", get_guard)
    core.handle(
        ModelCreateTicketRequest(title="Scratch", description=BODY, dry_run=True)
    )
    assert observed == [("remote-a", False)]


def test_locked_pillar_is_refused_from_remote_identity_even_on_operator_worker(
    monkeypatch, tmp_path: Path
):
    monkeypatch.delenv("ONEX_LANE", raising=False)
    monkeypatch.delenv("ONEX_REMOTE_LANE", raising=False)
    owners = tmp_path / "owners.yaml"
    owners.write_text(
        "pillars:\n  locked:\n    owner_linear_user_id: owner\n    assignable_by_lanes: false\n"
    )
    client, guard = Client(), Guard()
    core = handler(client, guard)
    core._pillar_owners_path = owners
    with pytest.raises(RuntimeError, match="refused for a lane"):
        core.handle(
            ModelCreateTicketRequest(title="Scratch", description=BODY, pillar="locked")
        )
    assert client.calls == guard.inputs == []
