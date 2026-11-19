# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every Linear call of the sprint closer is a request to node_linear_ticket_write_effect, sent over the event bus to
the runtime that hosts the node: no GraphQL, no Linear client, no key read and no in-process node in the closer."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from pydantic import JsonValue

from omnimarket.handlers.ticket.ticket_cli import load_ticket_topics
from omnimarket.models.scm import ModelScmBusReceipt, ModelScmTerminal
from omnimarket.models.ticket_write_io import ModelTicketWriteRequest
from omnimarket.nodes.node_sprint_closer_effect.handlers import (
    sprint_closer_sources as sources,
)
from omnimarket.nodes.node_sprint_closer_effect.handlers.sprint_closer_sources import (
    HandlerBusTicketCall,
    HandlerLinearTicketNode,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.ticket_bus_call import (
    TicketBusCallError,
)

pytestmark = pytest.mark.unit

CLOSER_ROOT = (
    Path(__file__).parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_sprint_closer_effect"
)


class RecordingRunner:
    """Stands in for the node: answers each operation the way the write node's result model does."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def __call__(self, payload: dict[str, object]) -> dict[str, object]:
        self.requests.append(payload)
        op = payload["operation"]
        if op == "list_open":
            return {"candidate_ids": ["OMN-2", "OMN-1"]}
        if op == "read":
            return {
                "ticket_id": payload["ticket_id"],
                "title": "t",
                "description_body": "d",
                "ticket_status": "In Review",
                "assignee": "Owner",
            }
        return {"status": "ok"}


def test_activities_call_nodes_every_linear_call_is_a_node_request(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    client = HandlerLinearTicketNode(runner, tmp_path)
    assert client.open_issue_ids("proj") == ["OMN-1", "OMN-2"]
    issue = client.read_issue("OMN-7")
    assert (issue.state, issue.assignee, issue.closed) == ("In Review", "Owner", False)
    client.comment("OMN-7", "actor: x\nbody")
    client.set_done("OMN-7")
    assert [r["operation"] for r in runner.requests] == [
        "list_open",
        "read",
        "comment",
        "transition",
    ]
    assert runner.requests[0]["project"] == "proj"
    assert runner.requests[3]["state"] == "Done"
    assert all(str(r["idempotency_key"]) for r in runner.requests)


def test_own_last_comment_is_kept_so_the_fingerprint_rule_can_read_it(
    tmp_path: Path,
) -> None:
    client = HandlerLinearTicketNode(RecordingRunner(), tmp_path)
    assert client.read_issue("OMN-7").comment_bodies == ()
    client.comment("OMN-7", "actor: x\nclosed by evidence")
    assert client.read_issue("OMN-7").comment_bodies == (
        "actor: x\nclosed by evidence",
    )
    assert client.read_issue("OMN-8").comment_bodies == ()


def _terminal(*, failed: bool, payload: dict[str, object]) -> ModelScmTerminal:
    topics = load_ticket_topics()
    return ModelScmTerminal(
        receipt=ModelScmBusReceipt(
            correlation_id=uuid4(),
            command_topic=topics.command,
            command_envelope_id=uuid4(),
            terminal_topic=topics.failure if failed else topics.success,
        ),
        failed=failed,
        payload=cast(dict[str, JsonValue], payload),
    )


class FakeBusCall:
    """The ticket bus call seam: records each request and answers with one terminal."""

    def __init__(self, answer: ModelScmTerminal | Exception) -> None:
        self.answer = answer
        self.requests: list[ModelTicketWriteRequest] = []

    def __call__(self, request: ModelTicketWriteRequest) -> ModelScmTerminal:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_bus_runner_sends_the_request_over_the_bus_and_returns_the_result() -> None:
    fake = FakeBusCall(
        _terminal(
            failed=False,
            payload={"outcome": "completed", "result": {"ticket_status": "Done"}},
        )
    )
    out = HandlerBusTicketCall(call=fake)(
        {"operation": "read", "ticket_id": "OMN-7", "idempotency_key": "k"}
    )
    assert out == {"ticket_status": "Done"}
    (sent,) = fake.requests
    assert sent.requested_by_lane == "sprint-closer-workflow"
    assert sent.operation.value == "read"
    assert sent.ticket_id == "OMN-7"


def test_a_failed_terminal_raises_with_the_nodes_reason_and_codes() -> None:
    fake = FakeBusCall(
        _terminal(
            failed=True,
            payload={
                "outcome": "failed",
                "reason": "guard_refused",
                "finding_codes": ["x"],
            },
        )
    )
    with pytest.raises(RuntimeError, match=r"guard_refused.*\['x'\]"):
        HandlerBusTicketCall(call=fake)(
            {
                "operation": "transition",
                "ticket_id": "OMN-7",
                "state": "Done",
                "idempotency_key": "k",
            }
        )


def test_no_terminal_raises_as_an_unknown_outcome_not_a_silent_success() -> None:
    fake = FakeBusCall(TicketBusCallError("read for OMN-7: no answer on lane 'dev'"))
    with pytest.raises(RuntimeError, match="unknown outcome"):
        HandlerBusTicketCall(call=fake)(
            {"operation": "read", "ticket_id": "OMN-7", "idempotency_key": "k"}
        )


def test_the_default_call_binds_the_dev_lane_and_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_bus_ticket_call(*, bus_lane: str, timeout_s: float) -> FakeBusCall:
        seen.update(bus_lane=bus_lane, timeout_s=timeout_s)
        return FakeBusCall(
            _terminal(
                failed=False,
                payload={"outcome": "completed", "result": {"ticket_status": "Todo"}},
            )
        )

    monkeypatch.setattr(sources, "bus_ticket_call", fake_bus_ticket_call)
    HandlerBusTicketCall()(
        {"operation": "read", "ticket_id": "OMN-7", "idempotency_key": "k"}
    )
    assert seen == {"bus_lane": "dev", "timeout_s": 90.0}


def test_the_closer_never_runs_the_ticket_node_in_process() -> None:
    for path in CLOSER_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert '"node",' not in text, path
        assert "HandlerLinearTicketWriteEffect" not in text, path


def test_the_closer_carries_no_direct_linear_access() -> None:
    for path in CLOSER_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "api.linear.app" not in text, path
        assert "urllib.request" not in text, path
        assert "LINEAR_API_KEY" not in text, path
