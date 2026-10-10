"""Source-bound identity, pre-render assignment and fail-closed controls."""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from omnibase_core.models.events.work.model_work_ledger_render import render_ledger_row
from pydantic import ValidationError

from omnimarket.events.model_ledger_row_event import (
    WORK_LEDGER_EVENT_NAMESPACE,
    work_ledger_event_id,
    work_ledger_row_id,
)
from omnimarket.nodes.node_projection_work_ledger.models import (
    ModelWorkLedgerProjectionInbound,
)
from tests.nodes.work_ledger_fixtures import records_for_all_ledger_row_types

RAW_ROW = "2026-09-28T11:00:00Z | CLAIM | lane=acceptance-lane | ticket=OMN-20001 | est ~1 lane-hours; displaces none; (OMN-20001) | paired source"
ROW_ID = "f924468623ecc6a418091c28b6836b189f4052f4cd4b09b8690df574f50fa94c"
EVENT_ID = UUID("3df28144-b920-5685-a88b-32be34cd9db7")


def paired_inbound() -> ModelWorkLedgerProjectionInbound:
    event = records_for_all_ledger_row_types()[1].event.model_copy(
        update={"event_id": EVENT_ID}
    )
    return ModelWorkLedgerProjectionInbound(
        ledger_id="rolling-work-ledger",
        row_id=ROW_ID,
        event_id=EVENT_ID,
        source="acceptance-test",
        raw_row=RAW_ROW,
        provenance_kind="markdown",
        event=event,
    )


def test_canonical_uuid5_fixed_vectors_and_source_normalization() -> None:
    assert str(WORK_LEDGER_EVENT_NAMESPACE) == "44cfd495-27b1-5bd6-960b-540950a90475"
    assert (
        uuid5(NAMESPACE_URL, "https://omninode.ai/namespaces/work-ledger/event-id/v1")
        == WORK_LEDGER_EVENT_NAMESPACE
    )
    assert (
        str(uuid5(WORK_LEDGER_EVENT_NAMESPACE, "rolling-work-ledger"))
        == "7701900f-d17e-5778-bccc-12d8c31d3af2"
    )
    assert work_ledger_row_id(" \n" + RAW_ROW + "\n ") == ROW_ID
    assert work_ledger_event_id("rolling-work-ledger", ROW_ID) == EVENT_ID
    assert work_ledger_event_id("other-ledger", ROW_ID) != EVENT_ID


def test_markdown_identity_is_assigned_before_typed_rendering() -> None:
    inbound = paired_inbound()
    rendered = render_ledger_row(inbound.event, {})
    assert f"event={EVENT_ID}" in rendered
    assert "src=typed" in rendered
    assert inbound.raw_row == RAW_ROW
    assert inbound.row_id == ROW_ID
    assert work_ledger_row_id(rendered) != ROW_ID
    assert (
        work_ledger_event_id(inbound.ledger_id, work_ledger_row_id(rendered))
        != EVENT_ID
    )


@pytest.mark.parametrize(
    "change",
    [
        {"event_id": UUID("00000000-0000-4000-8000-000000000999")},
        {"row_id": "0" * 64},
        {"raw_row": RAW_ROW + " changed"},
        {"raw_row": " " + RAW_ROW},
        {"row_schema": "work-ledger-event/1"},
    ],
)
def test_changed_source_or_uuid_is_refused(change: dict[str, object]) -> None:
    data = paired_inbound().model_dump()
    data.update(change)
    with pytest.raises(ValidationError):
        ModelWorkLedgerProjectionInbound.model_validate(data)


def test_typed_only_uuid_is_preserved_before_rendering() -> None:
    event = records_for_all_ledger_row_types()[1].event
    rendered = render_ledger_row(event, {})
    inbound = ModelWorkLedgerProjectionInbound(
        ledger_id="rolling-work-ledger",
        row_id=work_ledger_row_id(rendered),
        raw_row=rendered,
        event_id=event.event_id,
        source="acceptance-test",
        provenance_kind="typed",
        event=event,
    )
    assert inbound.event_id == event.event_id
    assert inbound.event_id != work_ledger_event_id(inbound.ledger_id, inbound.row_id)


@pytest.fixture
async def source_database(monkeypatch):
    """Use the existing integration provider, only an owned scratch schema.

    Real Postgres only: reads INTEGRATION_POSTGRES_* (or POSTGRES_PASSWORD) through
    ``_connect_or_skip`` and skips when no database is reachable.
    """
    from pathlib import Path
    from uuid import uuid4

    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
    from omnimarket.nodes.node_projection_work_ledger.handlers import (
        handler_work_ledger_projection as module,
    )
    from tests.test_work_ledger_real_postgres_write_path import _connect_or_skip, _dsn

    conn = await _connect_or_skip()
    schema = "m3_" + uuid4().hex
    migration_root = Path(module.__file__).parents[1] / "migrations"
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    for name in (
        "0000_create_work_ledger.sql",
        "0002_work_ledger_typed_records.sql",
        "0003_work_ledger_seq.sql",
    ):
        await conn.execute(
            (migration_root / name).read_text().replace("omninode_internal", schema)
        )
    for name, value in vars(module).copy().items():
        if (
            name.startswith("_")
            and isinstance(value, str)
            and "omninode_internal.work_ledger" in value
        ):
            monkeypatch.setattr(
                module, name, value.replace("omninode_internal", schema)
            )
    db = AsyncpgAdapter(dsn=_dsn(), min_size=1, max_size=2)
    await db.connect()
    try:
        yield db, conn, schema
    finally:
        await db.close()
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()


@pytest.mark.integration
async def test_real_markdown_and_typed_pair_reconcile_one_immutable_row(
    source_database,
    tmp_path,
):
    from omnibase_core.models.events.work.model_work_ledger_line import (
        dump_work_ledger_line,
    )

    from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
    from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
        WorkLedgerProjectionWriter,
    )
    from omnimarket.projection.runner import MessageMeta

    db, conn, schema = source_database
    writer = WorkLedgerProjectionWriter()
    writer._db = db
    meta = MessageMeta(
        partition=0, offset=1, fallback_id="", topic=EnumLedgerRowType.CLAIM.topic
    )
    from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
        HandlerEventEmitEffect,
    )
    from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox
    from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit import (
        HandlerWorkLedgerEmit,
    )
    from omnimarket.nodes.node_work_ledger_emit_effect.models.model_work_ledger_emit_request import (
        ModelWorkLedgerEmitRequest,
    )
    from tests.unit.nodes.node_work_ledger_emit_effect.test_handler_work_ledger_emit import (
        _Adapter,
    )

    adapter = _Adapter()
    producer = HandlerWorkLedgerEmit(
        emitter=HandlerEventEmitEffect(
            spool=SpoolOutbox(tmp_path / "spool"), publish_adapter=adapter
        )
    )
    inbound = paired_inbound()
    legacy_result = producer.handle(ModelWorkLedgerEmitRequest(row=RAW_ROW))
    typed_result = producer.handle(
        ModelWorkLedgerEmitRequest(row=RAW_ROW, event=inbound.event)
    )
    assert legacy_result.accepted
    assert typed_result.accepted
    assert len(adapter.published) == 2
    legacy_topic, legacy, legacy_key = adapter.published[0]
    typed_topic, typed, typed_key = adapter.published[1]
    assert legacy_topic == EnumLedgerRowType.CLAIM.topic
    assert typed_topic == EnumLedgerRowType.CLAIM.typed_topic
    assert legacy_key == typed_key == "rolling-work-ledger"
    assert legacy["event_id"] == typed["event_id"] == str(EVENT_ID)
    await writer._project_and_report(EnumLedgerRowType.CLAIM.topic, legacy, meta)
    await writer._project_and_report(EnumLedgerRowType.CLAIM.typed_topic, typed, meta)
    await writer._project_and_report(EnumLedgerRowType.CLAIM.topic, legacy, meta)
    await writer._project_and_report(EnumLedgerRowType.CLAIM.typed_topic, typed, meta)
    rows = await conn.fetch(f"SELECT * FROM {schema}.work_ledger_rows")
    assert len(rows) == 1
    assert rows[0]["row_id"] == ROW_ID
    assert rows[0]["event_id"] == EVENT_ID
    assert rows[0]["raw_row"] == RAW_ROW
    assert rows[0]["source"] == "onex-ledger"
    assert rows[0]["record"] == dump_work_ledger_line(inbound.to_record())


@pytest.mark.integration
async def test_changed_canonical_content_and_uuid_collision_rollback(source_database):
    import asyncpg

    from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
    from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
        WorkLedgerProjectionWriter,
    )
    from omnimarket.projection.runner import MessageMeta

    db, conn, schema = source_database
    writer = WorkLedgerProjectionWriter()
    writer._db = db
    inbound = paired_inbound()
    meta = MessageMeta(
        partition=0, offset=1, fallback_id="", topic=EnumLedgerRowType.CLAIM.typed_topic
    )
    await writer._project_and_report(meta.topic, inbound.model_dump(mode="json"), meta)
    changed_event = inbound.event.model_copy(
        update={"summary": "changed canonical content"}
    )
    changed = inbound.model_copy(update={"event": changed_event})
    with pytest.raises(ValueError, match="conflicting immutable"):
        await writer._project_and_report(
            meta.topic, changed.model_dump(mode="json"), meta
        )
    collision = inbound.model_copy(
        update={
            "raw_row": RAW_ROW + " new",
            "row_id": work_ledger_row_id(RAW_ROW + " new"),
            "provenance_kind": "typed",
        }
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await writer._project_and_report(
            meta.topic, collision.model_dump(mode="json"), meta
        )
    rows = await conn.fetch(f"SELECT * FROM {schema}.work_ledger_rows")
    assert len(rows) == 1
    assert rows[0]["raw_row"] == RAW_ROW
    from omnibase_core.models.events.work.model_work_ledger_line import (
        dump_work_ledger_line,
    )

    assert rows[0]["record"] == dump_work_ledger_line(inbound.to_record())


@pytest.mark.integration
async def test_question_failure_rolls_back_insert_and_legacy_backfill(source_database):
    import asyncpg

    from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
    from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
        WorkLedgerProjectionWriter,
    )
    from omnimarket.projection.runner import MessageMeta
    from tests.nodes.work_ledger_fixtures import question_records

    db, conn, schema = source_database
    writer = WorkLedgerProjectionWriter()
    writer._db = db
    meta = MessageMeta(
        partition=0, offset=1, fallback_id="", topic=EnumLedgerRowType.CLAIM.topic
    )
    await writer._project_and_report(meta.topic, {"raw_row": RAW_ROW}, meta)
    await conn.execute(
        f"ALTER TABLE {schema}.work_ledger_state ADD CONSTRAINT reject_question CHECK (kind <> 'question')"
    )
    event = question_records()[0].event
    raw = render_ledger_row(event, {})
    inbound = ModelWorkLedgerProjectionInbound(
        ledger_id="rolling-work-ledger",
        event_id=event.event_id,
        row_id=work_ledger_row_id(raw),
        raw_row=raw,
        source="test",
        provenance_kind="typed",
        event=event,
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await writer._project_and_report(
            EnumLedgerRowType.MSG.typed_topic, inbound.model_dump(mode="json"), meta
        )
    rows = await conn.fetch(
        f"SELECT row_id, event_id, record FROM {schema}.work_ledger_rows"
    )
    assert len(rows) == 1
    assert rows[0]["row_id"] == ROW_ID
    assert rows[0]["event_id"] is None
    assert rows[0]["record"] is None


@pytest.mark.integration
async def test_legacy_works_without_typed_migration_and_v2_fails_closed(
    source_database,
):
    import asyncpg

    from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
    from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
        WorkLedgerProjectionWriter,
    )
    from omnimarket.projection.runner import MessageMeta

    db, conn, schema = source_database
    await conn.execute(
        f"ALTER TABLE {schema}.work_ledger_rows DROP COLUMN event_id, DROP COLUMN record, DROP COLUMN provenance_kind"
    )
    writer = WorkLedgerProjectionWriter()
    writer._db = db
    meta = MessageMeta(
        partition=0, offset=1, fallback_id="", topic=EnumLedgerRowType.CLAIM.topic
    )
    await writer._project_and_report(
        meta.topic, {"raw_row": RAW_ROW, "row_id": ROW_ID, "event_id": ROW_ID}, meta
    )
    with pytest.raises(asyncpg.UndefinedColumnError):
        await writer._project_and_report(
            EnumLedgerRowType.CLAIM.typed_topic,
            paired_inbound().model_dump(mode="json"),
            meta,
        )
    assert await conn.fetchval(f"SELECT count(*) FROM {schema}.work_ledger_rows") == 1


@pytest.mark.integration
@pytest.mark.parametrize("mutation", ["ledger", "hash", "uuid"])
async def test_reconciliation_refuses_corrupt_legacy_rows_before_typed_write(
    source_database, mutation
):
    from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
    from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
        WorkLedgerProjectionWriter,
    )
    from omnimarket.projection.runner import MessageMeta

    db, conn, schema = source_database
    writer = WorkLedgerProjectionWriter()
    writer._db = db
    meta = MessageMeta(
        partition=0, offset=1, fallback_id="", topic=EnumLedgerRowType.CLAIM.topic
    )
    await writer._project_and_report(meta.topic, {"raw_row": RAW_ROW}, meta)
    if mutation == "ledger":
        await conn.execute(
            f"UPDATE {schema}.work_ledger_rows SET ledger_id='wrong-ledger'"
        )
    elif mutation == "hash":
        await conn.execute(
            f"UPDATE {schema}.work_ledger_rows SET raw_row=raw_row || ' changed'"
        )
    else:
        await conn.execute(
            f"UPDATE {schema}.work_ledger_rows SET event_id='00000000-0000-4000-8000-000000000999'"
        )
    with pytest.raises(ValueError, match=r"reconciliation|mismatch"):
        await writer._project_and_report(
            EnumLedgerRowType.CLAIM.typed_topic,
            paired_inbound().model_dump(mode="json"),
            meta,
        )
    assert (
        await conn.fetchval(
            f"SELECT count(*) FROM {schema}.work_ledger_rows WHERE record IS NOT NULL"
        )
        == 0
    )


@pytest.mark.integration
async def test_v2_refuses_legacy_body_and_topic_kind_mismatch(source_database):
    from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
    from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
        WorkLedgerProjectionWriter,
    )
    from omnimarket.projection.runner import MessageMeta

    db, conn, schema = source_database
    writer = WorkLedgerProjectionWriter()
    writer._db = db
    meta = MessageMeta(
        partition=0, offset=1, fallback_id="", topic=EnumLedgerRowType.CLAIM.typed_topic
    )
    with pytest.raises(ValidationError):
        await writer._project_and_report(
            meta.topic, {"raw_row": RAW_ROW, "row_id": ROW_ID}, meta
        )
    with pytest.raises(ValueError, match="topic/type mismatch"):
        await writer._project_and_report(
            EnumLedgerRowType.STATUS.typed_topic,
            paired_inbound().model_dump(mode="json"),
            meta,
        )
    assert await conn.fetchval(f"SELECT count(*) FROM {schema}.work_ledger_rows") == 0


@pytest.mark.integration
async def test_simultaneous_legacy_and_typed_delivery_has_one_safe_identity(
    source_database,
):
    import asyncio

    from omnibase_core.models.events.work.model_work_ledger_line import (
        dump_work_ledger_line,
    )

    from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
    from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
        WorkLedgerProjectionWriter,
    )
    from omnimarket.projection.runner import MessageMeta

    db, conn, schema = source_database
    writer = WorkLedgerProjectionWriter()
    writer._db = db
    inbound = paired_inbound()
    meta = MessageMeta(
        partition=0, offset=1, fallback_id="", topic=EnumLedgerRowType.CLAIM.topic
    )
    legacy = {
        "raw_row": RAW_ROW,
        "row_id": ROW_ID,
        "event_id": ROW_ID,
        "source": "onex-ledger",
    }
    for seed in range(6):
        await conn.execute(
            f"TRUNCATE {schema}.work_ledger_rows, {schema}.work_ledger_state"
        )
        calls = [
            writer._project_and_report(EnumLedgerRowType.CLAIM.topic, legacy, meta),
            writer._project_and_report(
                EnumLedgerRowType.CLAIM.typed_topic,
                inbound.model_dump(mode="json"),
                meta,
            ),
        ]
        if seed % 2:
            calls.reverse()
        await asyncio.gather(*calls)
        rows = await conn.fetch(f"SELECT * FROM {schema}.work_ledger_rows")
        assert len(rows) == 1
        assert rows[0]["event_id"] == EVENT_ID
        assert rows[0]["raw_row"] == RAW_ROW
        assert rows[0]["record"] == dump_work_ledger_line(inbound.to_record())
    bad_legacy = {**legacy, "event_id": "00000000-0000-4000-8000-000000000999"}
    with pytest.raises(ValueError, match="historical source hash"):
        await writer._project_and_report(
            EnumLedgerRowType.CLAIM.topic, bad_legacy, meta
        )


def test_registered_v2_producer_routes_all_eleven_core_row_types(tmp_path):
    from omnibase_core.models.events.work.model_work_ledger_render import (
        ROW_TYPE_BY_KIND,
    )

    from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
    from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
        HandlerEventEmitEffect,
    )
    from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox
    from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit import (
        HandlerWorkLedgerEmit,
    )
    from omnimarket.nodes.node_work_ledger_emit_effect.models.model_work_ledger_emit_request import (
        ModelWorkLedgerEmitRequest,
    )
    from tests.unit.nodes.node_work_ledger_emit_effect.test_handler_work_ledger_emit import (
        _Adapter,
    )

    adapter = _Adapter()
    producer = HandlerWorkLedgerEmit(
        emitter=HandlerEventEmitEffect(
            spool=SpoolOutbox(tmp_path / "spool"), publish_adapter=adapter
        )
    )
    records = records_for_all_ledger_row_types()
    index = {record.event.event_id: record.event for record in records}
    for record in records[1:]:
        event = record.event
        result = producer.handle(
            ModelWorkLedgerEmitRequest(
                row=render_ledger_row(event, index),
                event=event,
                provenance_kind="typed",
            )
        )
        assert result.accepted
        topic, payload, key = adapter.published[-1]
        row_type = EnumLedgerRowType(ROW_TYPE_BY_KIND[event.kind])
        assert topic == row_type.typed_topic
        assert key == "rolling-work-ledger"
        assert payload["event_id"] == str(event.event_id)
        assert payload["event"]["event_id"] == str(event.event_id)
    assert {topic for topic, _, _ in adapter.published} == {
        row_type.typed_topic for row_type in EnumLedgerRowType
    }
    # EPOCH is separately gated; it has no route in this eleven-topic family.
    refused = producer.handle(
        ModelWorkLedgerEmitRequest(
            row=render_ledger_row(records[0].event, index),
            event=records[0].event,
            provenance_kind="typed",
        )
    )
    assert not refused.accepted
    assert len(adapter.published) == 11


@pytest.mark.parametrize(
    "ledger_id", ["", " ", " rolling-work-ledger", "rolling-work-ledger "]
)
def test_noncanonical_config_and_typed_inbound_refuse_before_database(
    tmp_path, monkeypatch, ledger_id
):
    from pathlib import Path

    import yaml

    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
    from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
        WorkLedgerProjectionWriter,
    )
    from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_projection import (
        ModelWorkLedgerProjectionConfig,
    )

    connected = []

    async def unexpected_connect(self):
        connected.append(self)
        raise AssertionError("malformed configured ledger reached the database")

    monkeypatch.setattr(AsyncpgAdapter, "connect", unexpected_connect)
    with pytest.raises(ValidationError):
        ModelWorkLedgerProjectionConfig(ledger_id=ledger_id)
    data = paired_inbound().model_dump()
    data["ledger_id"] = ledger_id
    data["provenance_kind"] = "typed"
    with pytest.raises(ValidationError):
        ModelWorkLedgerProjectionInbound.model_validate(data)
    source = (
        Path(__file__).parents[2]
        / "src/omnimarket/nodes/node_projection_work_ledger/contract.yaml"
    )
    config = yaml.safe_load(source.read_text())
    config["work_ledger"]["ledger_id"] = ledger_id
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValidationError):
        WorkLedgerProjectionWriter(contract_path=contract_path)
    assert connected == []


@pytest.mark.integration
async def test_distinct_legacy_arrival_cannot_race_typed_reconciliation(
    source_database, monkeypatch
):
    import asyncio

    from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
    from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
        WorkLedgerProjectionWriter,
    )
    from omnimarket.projection.runner import MessageMeta

    db, conn, schema = source_database
    writer = WorkLedgerProjectionWriter()
    writer._db = db
    meta = MessageMeta(
        partition=0, offset=1, fallback_id="", topic=EnumLedgerRowType.CLAIM.topic
    )
    await writer._project_and_report(meta.topic, {"raw_row": RAW_ROW}, meta)
    reconciled, resume = asyncio.Event(), asyncio.Event()
    original_reconcile = writer._reconcile_rows

    async def pause_after_reconciliation(connection):
        await original_reconcile(connection)
        reconciled.set()
        await resume.wait()

    monkeypatch.setattr(writer, "_reconcile_rows", pause_after_reconciliation)
    typed_task = asyncio.create_task(
        writer._project_and_report(
            EnumLedgerRowType.CLAIM.typed_topic,
            paired_inbound().model_dump(mode="json"),
            meta,
        )
    )
    await asyncio.wait_for(reconciled.wait(), timeout=2)
    distinct_row = RAW_ROW.replace("11:00:00Z", "11:00:01Z")
    legacy_task = asyncio.create_task(
        writer._project_and_report(meta.topic, {"raw_row": distinct_row}, meta)
    )
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(legacy_task), timeout=0.1)
    finally:
        resume.set()
        await asyncio.gather(typed_task, legacy_task)
    monkeypatch.setattr(writer, "_reconcile_rows", original_reconcile)
    # This row arrived AFTER typed commit. Next typed transaction reconciles it.
    rows = await conn.fetch(f"SELECT row_id, event_id FROM {schema}.work_ledger_rows")
    assert len(rows) == 2
    assert next(row for row in rows if row["row_id"] == ROW_ID)["event_id"] == EVENT_ID
    await writer._project_and_report(
        EnumLedgerRowType.CLAIM.typed_topic,
        paired_inbound().model_dump(mode="json"),
        meta,
    )
    assert (
        await conn.fetchval(
            f"SELECT count(*) FROM {schema}.work_ledger_rows WHERE event_id IS NULL"
        )
        == 0
    )
