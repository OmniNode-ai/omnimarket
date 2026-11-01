# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_dod_criteria_check_effect (OMN-21676).

One command on the in-memory bus in, one completed event out, through the handler the node's
contract names, with the GitHub pull request, its contract and the ticket recorded. The
chain is the one the landing controller drives before a merge: a request for a head, the
verdict, and ``merge_blocked`` set for drift and set for anything unreadable.
"""

from __future__ import annotations

import base64
import importlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_dod_criteria_check_effect.handlers.handler_dod_criteria_check import (
    HandlerDodCriteriaCheck,
)
from omnimarket.nodes.node_dod_criteria_check_effect.models import (
    ModelDodCriteriaCheckRequest,
    ModelDodCriteriaCheckResult,
)
from tests.unit.nodes.node_dod_verify.omn_20858_ticket_reader import criteria_body
from tests.unit.nodes.node_dod_verify.test_omn_20858_criteria_drift import (
    _pinned_contract,
    _statements,
)

CMD_TOPIC = "onex.cmd.omnimarket.dod-criteria-check-requested.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.dod-criteria-check-completed.v1"
_HEAD = "e" * 40
_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_dod_criteria_check_effect/contract.yaml"
)


class _Github:
    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        if "/pulls/" in request.path:
            return ModelGithubHttpResponse(
                status=200,
                headers={},
                body={
                    "title": "feat(OMN-20999): x",
                    "state": "open",
                    "merged": False,
                    "head": {"sha": _HEAD},
                },
            )
        text = yaml.safe_dump(_pinned_contract())
        return ModelGithubHttpResponse(
            status=200,
            headers={},
            body={
                "encoding": "base64",
                "content": base64.b64encode(text.encode()).decode(),
            },
        )


class _Issue:
    def __init__(self, description: str) -> None:
        self.identifier = "OMN-20999"
        self.description = description


class _Tracker:
    def __init__(self, description: str) -> None:
        self._description = description

    async def get_issue(self, issue_id: str) -> _Issue:
        return _Issue(self._description)

    async def close(self) -> None:
        return None


def _handler(event_bus: EventBusInmemory, description: str) -> HandlerDodCriteriaCheck:
    async def factory() -> _Tracker:
        return _Tracker(description)

    return HandlerDodCriteriaCheck(
        transport=_Github(), tracker_factory=factory, event_bus=event_bus
    )


def test_the_contract_names_the_handler_and_the_topics() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    module = importlib.import_module(contract["handler"]["module"])
    assert getattr(module, contract["handler"]["class"]) is HandlerDodCriteriaCheck
    assert contract["event_bus"]["subscribe_topics"] == [CMD_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [COMPLETED_TOPIC]
    assert set(contract["secrets"]) == {"GITHUB_TOKEN", "LINEAR_API_KEY"}


@pytest.mark.parametrize(
    ("description", "verdict", "blocked"),
    [
        (criteria_body(_statements()), "pass", False),
        (
            criteria_body({**_statements(), "AC1": "AC1: changed -- falsifier: x"}),
            "drift",
            True,
        ),
    ],
)
async def test_a_command_in_a_completed_event_out(
    event_bus: EventBusInmemory, description: str, verdict: str, blocked: bool
) -> None:
    handler = _handler(event_bus, description)
    completed: list[dict[str, object]] = []

    async def on_completed(message: Any) -> None:
        completed.append(json.loads(message.value))

    async def on_command(message: Any) -> None:
        payload = json.loads(message.value)
        await handler.handle(ModelDodCriteriaCheckRequest(**payload))

    await event_bus.start()
    await event_bus.subscribe(COMPLETED_TOPIC, on_message=on_completed, group_id="done")
    await event_bus.subscribe(CMD_TOPIC, on_message=on_command, group_id="check")
    request = {
        "correlation_id": str(uuid4()),
        "repo": "OmniNode-ai/omnibase_infra",
        "pr_number": 7,
        "head_sha": _HEAD,
    }
    await event_bus.publish(CMD_TOPIC, key=None, value=json.dumps(request).encode())

    assert len(completed) == 1
    result = ModelDodCriteriaCheckResult(**completed[0])
    assert result.verdict.value == verdict
    assert result.merge_blocked is blocked
    assert result.head_sha == _HEAD
    await event_bus.close()
