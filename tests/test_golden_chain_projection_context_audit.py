# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_projection_context_audit.

Replays context-integrity audit events through the writer and checks the two
things the plan's falsifier names: the node writes the rows the retired consumer
module wrote, and a second replay leaves one row per key.

The database here is a stand-in that applies the writer's ON CONFLICT
(event_key) rule to an in-memory table. The writer's SQL against real Postgres
is proven in tests/test_projection_context_audit_real_postgres.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.models.event_bus.model_consumer_group_handover import (
    ModelConsumerGroupHandover,
)
from pydantic import ValidationError

from omnimarket.nodes.node_projection_context_audit.handlers import (
    handler_projection_context_audit as fold_module,
)
from omnimarket.nodes.node_projection_context_audit.handlers.handler_context_audit_writer import (
    ContextAuditProjectionWriter,
)
from omnimarket.nodes.node_projection_context_audit.models import (
    ModelContextAuditEventWire,
)
from omnimarket.projection.runner import MessageMeta

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_context_audit/contract.yaml"
)

# The node's declared output topic (its contract's terminal_event).
PROJECTION_APPLIED_TOPIC = "onex.evt.omnimarket.projection-context-audit-applied.v1"

# The five topics the retired consumer's ConfigContextAuditConsumer.topics listed.
RETIRED_MODULE_TOPICS = [
    "onex.evt.omniclaude.audit-dispatch-validated.v1",
    "onex.evt.omniclaude.audit-scope-violation.v1",
    "onex.evt.omniclaude.audit-context-budget-exceeded.v1",
    "onex.evt.omniclaude.audit-return-bounded.v1",
    "onex.evt.omniclaude.audit-compression-triggered.v1",
]
# The retired writer's INSERT column list, in order, minus the table defaults.
RETIRED_MODULE_COLUMNS = (
    "task_id",
    "parent_task_id",
    "correlation_id",
    "contract_id",
    "event_type",
    "enforcement_level",
    "enforcement_action",
    "violation_details",
    "context_tokens_used",
    "context_budget_tokens",
    "return_tokens",
    "return_max_tokens",
)


class _KeyedTable:
    """`execute` for the writer's UPSERT: one row per event_key, ids from a serial."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self._next_id = 1

    async def execute(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        assert "ON CONFLICT (event_key) DO UPDATE" in sql
        key = params[0]
        columns = ("event_key", *RETIRED_MODULE_COLUMNS)
        assert len(params) == len(columns)
        inserted = key not in self.rows
        row = dict(zip(columns, params, strict=True))
        if inserted:
            row["id"] = self._next_id
            self._next_id += 1
        else:
            row["id"] = self.rows[key]["id"]
        self.rows[key] = row
        return [{"id": row["id"], "event_key": key, "event_inserted": inserted}]


def _event(index: int) -> dict[str, Any]:
    """An audit event payload, derived from the node's wire model."""
    odd = bool(index % 2)
    wire = ModelContextAuditEventWire(
        task_id=f"task-{index}",
        parent_task_id=None if odd else f"task-{index - 1}",
        correlation_id=f"corr-{index}",
        contract_id=f"OMN-{index}",
        event_type=f"event-{index}",
        enforcement_level="strict",
        enforcement_action="block" if odd else None,
        violation_details={"reason": "over budget", "n": index} if odd else None,
        context_tokens_used=100 * index,
        context_budget_tokens=1000,
        return_tokens=index,
        return_max_tokens=50,
    )
    payload = wire.model_dump(mode="json")
    # An unknown key: the retired module ignored it, and so does this node.
    payload["extra_producer_field"] = "ignored"
    return payload


def _stream() -> list[tuple[str, MessageMeta, dict[str, Any]]]:
    return [
        (
            topic,
            MessageMeta(
                partition=index % 2,
                offset=10 + index,
                fallback_id=f"fallback-{index}",
                topic=topic,
            ),
            _event(index),
        )
        for index, topic in enumerate(RETIRED_MODULE_TOPICS)
    ]


def _writer_on(
    db: _KeyedTable, monkeypatch: pytest.MonkeyPatch
) -> ContextAuditProjectionWriter:
    writer = ContextAuditProjectionWriter()
    monkeypatch.setattr(writer, "_db", db)
    return writer


async def _replay(writer: ContextAuditProjectionWriter) -> None:
    for topic, meta, event in _stream():
        await writer.project_event(topic, event, meta)


def _expected_retired_module_row(event: dict[str, Any]) -> dict[str, Any]:
    """What the retired WriterContextAuditPostgres bound for this event."""
    details = event.get("violation_details")
    return {
        "task_id": event["task_id"],
        "parent_task_id": event.get("parent_task_id"),
        "correlation_id": event["correlation_id"],
        "contract_id": event.get("contract_id"),
        "event_type": event["event_type"],
        "enforcement_level": event["enforcement_level"],
        "enforcement_action": event.get("enforcement_action"),
        "violation_details": json.dumps(details) if details is not None else None,
        "context_tokens_used": event.get("context_tokens_used"),
        "context_budget_tokens": event.get("context_budget_tokens"),
        "return_tokens": event.get("return_tokens"),
        "return_max_tokens": event.get("return_max_tokens"),
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replay_writes_the_same_rows_and_a_second_replay_adds_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _KeyedTable()
    writer = _writer_on(db, monkeypatch)

    await _replay(writer)

    assert len(db.rows) == len(RETIRED_MODULE_TOPICS)
    first_ids = {key: row["id"] for key, row in db.rows.items()}
    written = sorted(
        (
            {column: row[column] for column in RETIRED_MODULE_COLUMNS}
            for row in db.rows.values()
        ),
        key=lambda row: row["task_id"],
    )
    expected = sorted(
        (_expected_retired_module_row(event) for _, _, event in _stream()),
        key=lambda row: row["task_id"],
    )
    # Byte for byte, violation_details text included: the retired writer bound
    # json.dumps(details) and so does the fold.
    assert written == expected

    await _replay(writer)

    assert len(db.rows) == len(RETIRED_MODULE_TOPICS)
    assert {key: row["id"] for key, row in db.rows.items()} == first_ids


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_events_with_equal_content_at_different_offsets_keep_two_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _KeyedTable()
    writer = _writer_on(db, monkeypatch)
    topic = RETIRED_MODULE_TOPICS[0]
    event = _event(1)

    for offset in (5, 6):
        await writer.project_event(
            topic,
            event,
            MessageMeta(partition=0, offset=offset, fallback_id="f", topic=topic),
        )

    assert len(db.rows) == 2


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing", ["task_id", "correlation_id", "event_type", "enforcement_level"]
)
async def test_an_event_missing_a_required_field_is_refused_before_any_write(
    missing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _KeyedTable()
    writer = _writer_on(db, monkeypatch)
    topic = RETIRED_MODULE_TOPICS[0]
    event = _event(1)
    del event[missing]

    with pytest.raises(ValidationError):
        await writer.project_event(
            topic,
            event,
            MessageMeta(partition=0, offset=1, fallback_id="f", topic=topic),
        )

    assert db.rows == {}


@pytest.mark.unit
def test_the_writer_declares_in_process_dispatch() -> None:
    # Undeclared, the shared runtime skips the writer: offsets advance, zero rows.
    assert ContextAuditProjectionWriter.onex_runtime_inprocess_dispatch is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "missing", ["task_id", "correlation_id", "event_type", "enforcement_level"]
)
def test_runtime_refuses_malformed_input_before_connecting(
    missing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = ContextAuditProjectionWriter()
    event = _event(1)
    del event[missing]

    async def unavailable() -> None:
        pytest.fail("malformed input must be classified before database I/O")

    monkeypatch.setattr(writer.db, "connect", unavailable)
    with pytest.raises(ValidationError):
        writer.handle(
            {**event, "_topic": RETIRED_MODULE_TOPICS[0], "_partition": 0, "_offset": 1}
        )


@pytest.mark.unit
def test_runtime_replay_writes_and_closes_each_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _KeyedTable()
    writer = ContextAuditProjectionWriter()
    calls: list[str] = []

    async def connect() -> None:
        calls.append("connect")

    async def execute(sql: str, *params: Any) -> list[dict[str, Any]]:
        calls.append("write")
        return await db.execute(sql, *params)

    async def close() -> None:
        calls.append("close")

    monkeypatch.setattr(writer.db, "connect", connect)
    monkeypatch.setattr(writer.db, "execute", execute)
    monkeypatch.setattr(writer.db, "close", close)
    for inserted in (True, False):
        for topic, meta, event in _stream():
            result = writer.handle(
                {
                    **event,
                    "_topic": topic,
                    "_partition": meta.partition,
                    "_offset": meta.offset,
                }
            )
            assert result["rows_upserted"] == 1
            assert result["event_inserted"] is inserted
            assert result["event_key"] in db.rows
    assert len(db.rows) == len(RETIRED_MODULE_TOPICS)
    assert calls == ["connect", "write", "close"] * (2 * len(RETIRED_MODULE_TOPICS))


@pytest.mark.unit
def test_contract_declares_the_retired_modules_topics_and_a_dlq() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["event_bus"]["subscribe_topics"] == RETIRED_MODULE_TOPICS
    assert contract["event_bus"]["dlq_topics"] == [
        "onex.dlq.omnimarket.projection-context-audit-malformed.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [PROJECTION_APPLIED_TOPIC]
    assert contract["terminal_event"] == PROJECTION_APPLIED_TOPIC
    assert contract["db_io"]["dedupe_key"] == ["event_key"]
    assert ContextAuditProjectionWriter().subscribe_topics == RETIRED_MODULE_TOPICS


@pytest.mark.unit
def test_the_handover_names_the_group_the_retired_consumer_commits_under() -> None:
    # The retired module's ConfigContextAuditConsumer.kafka_group_id default is the
    # literal `context-audit-postgres` and its auto_offset_reset default is
    # `earliest`; neither the catalog service nor the dev compose files override
    # them. A literal group is named through from_group_id (OMN-21932).
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))

    handover = ModelConsumerGroupHandover.model_validate(
        contract["consumer_group_handover"]
    )

    assert handover.from_group_id == "context-audit-postgres"
    assert handover.from_auto_offset_reset == "earliest"
    assert handover.handover_rule == "copy_if_uncommitted"
    assert handover.source_group("dev") == "context-audit-postgres"


@pytest.mark.unit
def test_a_migration_delivers_the_tenant_projection_writer_grants() -> None:
    # The topology grant delivery gate refuses a declared table grant with no
    # delivering migration, and a BIGSERIAL id needs its sequence granted too.
    migration = (
        CONTRACT_PATH.parent
        / "migrations"
        / "0001_grant_tenant_projection_writer_context_audit_events.sql"
    ).read_text(encoding="utf-8")

    assert (
        "GRANT SELECT, INSERT, UPDATE ON public.context_audit_events "
        "TO tenant_projection_writer;"
    ) in migration
    assert (
        "GRANT USAGE ON SEQUENCE public.context_audit_events_id_seq "
        "TO tenant_projection_writer;"
    ) in migration


RECORDED_EVENTS_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "context_audit"
    / "recorded_audit_events.json"
)


def _recorded() -> dict[str, Any]:
    return json.loads(RECORDED_EVENTS_PATH.read_text(encoding="utf-8"))


async def _bound_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, list[Any], list[Any]]]:
    """For each recorded event: the retired writer's parameters and the node's."""
    recorded = _recorded()
    db = _KeyedTable()
    writer = _writer_on(db, monkeypatch)
    results: list[tuple[str, list[Any], list[Any]]] = []
    for item in recorded["events"]:
        db.rows.clear()
        await writer.project_event(
            item["topic"],
            dict(item["value"]),
            MessageMeta(
                partition=item["partition"],
                offset=item["offset"],
                fallback_id="recorded",
                topic=item["topic"],
            ),
        )
        (row,) = db.rows.values()
        node_params = [row[column] for column in recorded["columns"]]
        results.append(
            (item["value"]["task_id"], item["old_writer_params"], node_params)
        )
    return results


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recorded_events_give_the_parameters_the_retired_writer_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Events in, the same projection rows out, byte for byte, on recorded input.

    The fixture holds recorded audit events and, beside each, the twelve
    parameters the retired WriterContextAuditPostgres bound for it (captured from
    that writer through a recording pool). The comparison is exact: values, None
    versus a JSON null, and the violation_details text.
    """
    recorded = _recorded()
    assert len(recorded["events"]) >= 6, "positive control: the recording is not empty"
    assert {item["topic"] for item in recorded["events"]} == set(RETIRED_MODULE_TOPICS)

    for task_id, old_params, node_params in await _bound_parameters(monkeypatch):
        assert node_params == old_params, task_id
        assert [type(v) for v in node_params] == [type(v) for v in old_params], task_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_parity_read_fails_when_the_fold_re_encodes_violation_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected break 1: the first draft's sorted, compact encoding of the details."""
    real_dumps = json.dumps
    monkeypatch.setattr(
        fold_module.json,
        "dumps",
        lambda value: real_dumps(value, sort_keys=True, separators=(",", ":")),
    )

    mismatched = [
        task_id
        for task_id, old_params, node_params in await _bound_parameters(monkeypatch)
        if node_params != old_params
    ]

    assert mismatched, "the byte-for-byte read must go red on a re-encoded detail"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_parity_read_fails_when_the_fold_drops_a_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected break 2: the fold forgets the enforcement action."""
    real_handle = fold_module.HandlerProjectionContextAudit.handle

    def handle_without_action(self: Any, request: Any) -> Any:
        return real_handle(self, request).model_copy(
            update={"enforcement_action": None}
        )

    monkeypatch.setattr(
        fold_module.HandlerProjectionContextAudit, "handle", handle_without_action
    )

    mismatched = [
        task_id
        for task_id, old_params, node_params in await _bound_parameters(monkeypatch)
        if node_params != old_params
    ]

    assert mismatched, "the byte-for-byte read must go red on a dropped column"
