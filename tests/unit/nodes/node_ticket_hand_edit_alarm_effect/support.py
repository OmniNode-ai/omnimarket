# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders, recording doubles and a rig for the ticket hand edit alarm tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_ticket_contract_render_compute.handlers.handler_ticket_contract_render import (
    HandlerTicketContractRender,
)
from omnimarket.nodes.node_ticket_contract_render_compute.models import (
    EnumTicketContractRenderOperation,
    ModelTicketContractRenderRequest,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers import (
    handler_ticket_hand_edit_alarm,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.compare_ticket import (
    content_sha256,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.handler_ticket_hand_edit_alarm import (
    HandlerTicketHandEditAlarm,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.store_ticket_hand_edit import (
    TicketHandEditStore,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    ModelDescriptionRevision,
    ModelLinearIssueSnapshot,
    ModelTicketContractWritten,
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
TICKET = "OMN-90001"
NODE_ACTOR = "example-node-actor"
HAND = "example-person-actor"
SETTINGS: dict[str, object] = {
    "check_interval_seconds": 60,
    "node_actor_id": NODE_ACTOR,
    "initial_lookback_seconds": 600,
    "write_grace_seconds": 30,
    "normalizations": [],
}
SLACK_SUFFIX = "slack-publish.v1"
FINDING_SUFFIX = "ticket-hand-edit.v1"
STATEMENT = "The unit holds."
FILING = ("created-by-lane: fixture-lane-0001",)
TITLE = "A fixture ticket"


def _render() -> tuple[str, str]:
    contract = next(iter(fixture_contracts().values()))
    result = HandlerTicketContractRender().handle(
        ModelTicketContractRenderRequest(
            operation=EnumTicketContractRenderOperation.RENDER,
            contract=contract,
            filing_lines=FILING,
        )
    )
    assert result.body, result.findings
    return result.body, result.block_fingerprint


WRITTEN_BODY, WRITTEN_FINGERPRINT = _render()
HAND_EDITED_BODY = WRITTEN_BODY.replace(STATEMENT, "Someone changed this by hand.")
assert HAND_EDITED_BODY != WRITTEN_BODY


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


def issue(
    description: str, *, updated_minutes_ago: float = 5, title: str = TITLE
) -> ModelLinearIssueSnapshot:
    return ModelLinearIssueSnapshot(
        ticket_id=TICKET,
        issue_uuid="uuid-90001",
        title=title,
        description=description,
        updated_at=ago(updated_minutes_ago),
    )


def written(
    description: str = WRITTEN_BODY,
    *,
    minutes_ago: float = 20,
    fingerprint: str = WRITTEN_FINGERPRINT,
    title: str = TITLE,
) -> ModelTicketContractWritten:
    return ModelTicketContractWritten(
        ticket_id=TICKET,
        contract_fingerprint=fingerprint,
        content_sha256=content_sha256(title, description),
        linear_updated_at=ago(minutes_ago),
    )


def revision(minutes_ago: float, *actors: str) -> ModelDescriptionRevision:
    return ModelDescriptionRevision(snapshot_at=ago(minutes_ago), actor_ids=actors)


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.value = at.timestamp()

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Publisher:
    def __init__(self, error: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.error = error

    def __call__(self, topic: str, payload: bytes) -> None:
        if self.error:
            raise RuntimeError(self.error)
        self.calls.append((topic, json.loads(payload)))

    def on(self, suffix: str) -> list[dict[str, object]]:
        return [body for topic, body in self.calls if topic.endswith(suffix)]


class Source:
    """A recorded Linear reply: the changed issues and the description history per ticket."""

    def __init__(
        self,
        issues: list[ModelLinearIssueSnapshot] | None = None,
        revisions: list[ModelDescriptionRevision] | None = None,
        *,
        list_error: str | None = None,
        history_error: str | None = None,
    ) -> None:
        self.issues = issues or []
        self.revisions = revisions or []
        self.list_error = list_error
        self.history_error = history_error
        self.list_calls: list[datetime] = []
        self.history_calls = 0

    def list_issues_updated_after(
        self, updated_after: datetime
    ) -> list[ModelLinearIssueSnapshot]:
        self.list_calls.append(updated_after)
        if self.list_error:
            raise RuntimeError(self.list_error)
        return [i for i in self.issues if i.updated_at > updated_after]

    def description_revisions(
        self, issue: ModelLinearIssueSnapshot
    ) -> list[ModelDescriptionRevision]:
        self.history_calls += 1
        if self.history_error:
            raise RuntimeError(self.history_error)
        return list(self.revisions)


class Rig:
    def __init__(
        self,
        handler: HandlerTicketHandEditAlarm,
        publisher: Publisher,
        database: InmemoryDatabaseAdapter,
        source: Source,
        clock: Clock,
    ) -> None:
        self.handler = handler
        self.publisher = publisher
        self.database = database
        self.source = source
        self.clock = clock
        self.store = TicketHandEditStore(database)

    def sweep(self, *, advance_seconds: float = 0) -> ModelTicketHandEditResult:
        self.clock.advance(advance_seconds)
        return self.handler.handle(ModelTicketHandEditSweepTrigger())

    def clock_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), tz=UTC)

    def clock_datetime_minus(self, seconds: float) -> datetime:
        return self.clock_datetime() - timedelta(seconds=seconds)

    def rows(self, table: str) -> list[dict[str, object]]:
        return self.database.query(table, None)


def make_rig(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    source: Source | None = None,
    settings: dict[str, object] | None = None,
    publisher: Publisher | None = None,
    database: InmemoryDatabaseAdapter | None = None,
) -> Rig:
    install_node_overlay(monkeypatch, root, NODE, settings or SETTINGS)
    monkeypatch.setattr(
        handler_ticket_hand_edit_alarm,
        "resolve_api_key_loop_safe",
        lambda _ref, **_kw: SecretStr("C0TESTCHANNEL"),
    )
    publisher = publisher if publisher is not None else Publisher()
    database = database if database is not None else InmemoryDatabaseAdapter()
    source = source if source is not None else Source()
    clock = Clock()
    handler = HandlerTicketHandEditAlarm(
        event_publisher=publisher, database=database, source=source, clock=clock
    )
    return Rig(handler, publisher, database, source, clock)
