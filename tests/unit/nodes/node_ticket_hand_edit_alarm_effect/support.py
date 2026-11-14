# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded Linear replies, recording doubles and a driver for the hand-edit alarm tests."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from omnimarket.models.model_ticket_contract_written import (
    ModelTicketContractWritten,
    ticket_content_sha256,
)
from omnimarket.nodes.node_ticket_contract_render_compute import (
    NodeTicketContractRenderCompute,
)
from omnimarket.nodes.node_ticket_contract_render_compute.models import (
    EnumTicketContractRenderOperation,
    ModelTicketContractRenderRequest,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers import (
    handler_ticket_hand_edit_alarm,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.handler_ticket_hand_edit_alarm import (
    HandlerTicketHandEditAlarm,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.linear_issue_reader import (
    decode_issues,
    decode_revisions,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    ModelDescriptionRevision,
    ModelLinearIssueSnapshot,
    ModelTicketHandEditResult,
    ModelTicketHandEditSweepTrigger,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from tests.node_overlay_support import install_node_overlay
from tests.nodes.node_ticket_contract_render_compute.contract_fixtures import (
    fixture_contracts,
)

NODE = "node_ticket_hand_edit_alarm_effect"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
WRITTEN_AT = NOW - timedelta(minutes=10)
EDITED_AT = NOW - timedelta(minutes=2)
NODE_ACTOR = "node-actor-id"
PERSON = "person-actor-id"
DOCUMENT = "document-1"
SETTINGS: dict[str, object] = {
    "check_interval_seconds": 60,
    "initial_lookback_seconds": 3600,
    "max_pages": 3,
}
SLACK_SUFFIX = "slack-publish.v1"
FINDING_SUFFIX = "ticket-hand-edit-finding.v1"


def standard_ticket() -> tuple[str, str, str]:
    """(identifier, title, body) of one standard ticket rendered by the render node."""
    contract = next(iter(fixture_contracts().values()))
    result = NodeTicketContractRenderCompute().handle(
        ModelTicketContractRenderRequest(
            operation=EnumTicketContractRenderOperation.RENDER,
            contract=contract,
            filing_lines=("created-by-lane: fixture-lane",),
        )
    )
    assert result.body, result.findings
    return contract.ticket_id, contract.title, result.body


def fingerprint_of(body: str) -> str:
    parsed = NodeTicketContractRenderCompute().handle(
        ModelTicketContractRenderRequest(
            operation=EnumTicketContractRenderOperation.PARSE, body=body
        )
    )
    return parsed.recomputed_fingerprint


def written(
    ticket_id: str, title: str, body: str, *, at: datetime = WRITTEN_AT
) -> ModelTicketContractWritten:
    return ModelTicketContractWritten(
        ticket_id=ticket_id,
        contract_fingerprint=fingerprint_of(body),
        content_sha256=ticket_content_sha256(title, body),
        updated_at=at,
    )


def issues_reply(*issues: tuple[str, str, str, datetime]) -> dict[str, Any]:
    """A recorded Linear reply of the issues list read."""
    return {
        "data": {
            "issues": {
                "nodes": [
                    {
                        "identifier": identifier,
                        "title": title,
                        "description": body,
                        "updatedAt": at.isoformat().replace("+00:00", "Z"),
                        "documentContent": {"id": DOCUMENT},
                    }
                    for identifier, title, body, at in issues
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
    }


def history_reply(*revisions: tuple[datetime, Sequence[str]]) -> dict[str, Any]:
    """A recorded Linear reply of documentContentHistory."""
    return {
        "data": {
            "documentContentHistory": {
                "history": [
                    {
                        "id": f"rev-{index}",
                        "contentDataSnapshotAt": at.isoformat().replace("+00:00", "Z"),
                        "actorIds": list(actors),
                        "contentData": {},
                    }
                    for index, (at, actors) in enumerate(revisions)
                ]
            }
        }
    }


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.value = at.timestamp()

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Publisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, topic: str, payload: bytes) -> None:
        self.calls.append((topic, json.loads(payload)))

    def on(self, suffix: str) -> list[dict[str, object]]:
        return [body for topic, body in self.calls if topic.endswith(suffix)]


class Reader:
    """Serves recorded Linear replies through the gateway's own decoding."""

    def __init__(self) -> None:
        self.issues: dict[str, Any] = issues_reply()
        self.history: dict[str, Any] = history_reply()
        self.issues_error: str | None = None
        self.cursors: list[datetime] = []

    def issues_updated_after(
        self, cursor: datetime, *, max_pages: int
    ) -> list[ModelLinearIssueSnapshot]:
        self.cursors.append(cursor)
        if self.issues_error:
            raise RuntimeError(self.issues_error)
        page, _ = decode_issues(self.issues)
        return [issue for issue in page if issue.updated_at > cursor]

    def description_revisions(
        self, document_content_id: str
    ) -> list[ModelDescriptionRevision]:
        return decode_revisions(self.history)


class Rig:
    def __init__(
        self,
        handler: HandlerTicketHandEditAlarm,
        publisher: Publisher,
        database: InmemoryDatabaseAdapter,
        reader: Reader,
        clock: Clock,
    ) -> None:
        self.handler = handler
        self.publisher = publisher
        self.database = database
        self.reader = reader
        self.clock = clock

    def sweep(self) -> ModelTicketHandEditResult:
        result = self.handler.handle(ModelTicketHandEditSweepTrigger())
        self.clock.advance(61)
        return result

    def record(self, event: ModelTicketContractWritten) -> ModelTicketHandEditResult:
        return self.handler.handle(event)

    def rows(self, table: str) -> list[dict[str, object]]:
        return self.database.query(table, None)


def make_rig(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    settings: dict[str, object] | None = None,
) -> Rig:
    install_node_overlay(monkeypatch, root, NODE, settings or SETTINGS)
    monkeypatch.setattr(
        handler_ticket_hand_edit_alarm,
        "resolve_api_key_loop_safe",
        lambda _ref, **_kw: SecretStr("C0TESTCHANNEL"),
    )
    publisher = Publisher()
    database = InmemoryDatabaseAdapter()
    reader = Reader()
    clock = Clock()
    handler = HandlerTicketHandEditAlarm(
        event_publisher=publisher, database=database, reader=reader, clock=clock
    )
    return Rig(handler, publisher, database, reader, clock)
