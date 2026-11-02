# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_lab_receipt_check_effect (OMN-21826).

One command on the in-memory bus in, one completed event out, through the handler the node's
contract names, with the pull request, its files, the receipts and the lab recorded. The chain is
the one a caller drives before a merge: a request for a head and a phase, the verdict, and
``merge_blocked`` set for a refusal and for anything unreadable.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_lab_receipt_check_effect.handlers.handler_lab_receipt_check import (
    HandlerLabReceiptCheck,
)
from omnimarket.nodes.node_lab_receipt_check_effect.models import (
    ModelLabReceiptCheckRequest,
)
from tests.nodes.node_lab_receipt_check_effect.support import (
    InMemoryCarrier,
    InMemoryLab,
    RecordedGithub,
    load_document,
)

pytestmark = pytest.mark.unit

CMD_TOPIC = "onex.cmd.omnimarket.lab-receipt-check-requested.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.lab-receipt-check-completed.v1"


@pytest.mark.parametrize(
    ("fixture", "phase", "verdict", "blocked"),
    [
        ("clean_receipt_not_on_lab", "verify", "pass", False),
        ("exempt_docs_only", "present", "not_required", False),
        ("violation_receipt_required", "present", "refused", True),
        ("violation_receipt_not_on_lab", "verify", "refused", True),
    ],
)
async def test_a_command_in_a_completed_event_out(
    event_bus: EventBusInmemory, fixture: str, phase: str, verdict: str, blocked: bool
) -> None:
    document = load_document(fixture)
    handler = HandlerLabReceiptCheck(
        transport=RecordedGithub(document),
        carrier=InMemoryCarrier(document.facts.receipts),
        lab_reader=InMemoryLab(document.facts.lab_reads),
        event_bus=event_bus,
    )
    completed: list[dict[str, object]] = []

    async def on_completed(message: Any) -> None:
        completed.append(json.loads(message.value))

    async def on_command(message: Any) -> None:
        await handler.handle(ModelLabReceiptCheckRequest(**json.loads(message.value)))

    await event_bus.start()
    await event_bus.subscribe(COMPLETED_TOPIC, on_message=on_completed, group_id="done")
    await event_bus.subscribe(CMD_TOPIC, on_message=on_command, group_id="check")
    request = {
        "correlation_id": str(uuid4()),
        "repo": document.facts.repository,
        "pr_number": 9,
        "head_sha": document.facts.head_sha,
        "phase": phase,
    }
    await event_bus.publish(CMD_TOPIC, key=b"k", value=json.dumps(request).encode())
    assert len(completed) == 1
    assert completed[0]["verdict"] == verdict
    assert completed[0]["merge_blocked"] is blocked
    assert completed[0]["phase"] == phase
    assert completed[0]["correlation_id"] == request["correlation_id"]


async def test_an_unreadable_pull_request_is_a_blocked_completed_event(
    event_bus: EventBusInmemory,
) -> None:
    document = load_document("clean_receipt_required")
    handler = HandlerLabReceiptCheck(
        transport=RecordedGithub(document, pr_head="f" * 40),
        carrier=InMemoryCarrier(document.facts.receipts),
        event_bus=event_bus,
    )
    completed: list[dict[str, object]] = []

    async def on_completed(message: Any) -> None:
        completed.append(json.loads(message.value))

    async def on_command(message: Any) -> None:
        await handler.handle(ModelLabReceiptCheckRequest(**json.loads(message.value)))

    await event_bus.start()
    await event_bus.subscribe(COMPLETED_TOPIC, on_message=on_completed, group_id="done")
    await event_bus.subscribe(CMD_TOPIC, on_message=on_command, group_id="check")
    request = {
        "correlation_id": str(uuid4()),
        "repo": document.facts.repository,
        "pr_number": 9,
        "head_sha": document.facts.head_sha,
        "phase": "present",
    }
    await event_bus.publish(CMD_TOPIC, key=b"k", value=json.dumps(request).encode())
    assert [c["verdict"] for c in completed] == ["unreadable"]
    assert completed[0]["merge_blocked"] is True
