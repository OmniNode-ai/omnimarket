# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for node_projection_dlq_redrive_effect (OMN-22811)."""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

from omnimarket.models.model_message_meta import MessageMeta
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_dlq_redrive_effect.handlers.handler_projection_dlq_redrive import (
    HandlerProjectionDlqRedrive,
    contract_dlq_topic,
)
from omnimarket.nodes.node_projection_dlq_redrive_effect.models import (
    EnumRedriveOutcome,
    EnumRedriveVerdict,
    ModelProjectionDlqRedriveRequest,
)

pytestmark = pytest.mark.unit

SOURCE_TOPIC = "onex.evt.omnimarket.delegate-skill-completed.v1"
REGISTERED = "tenant-registered"
UNKNOWN = "tenant-not-yet-registered"


def _envelope(cid: str, *, partition: int | None = 0, offset: int | None = 0) -> bytes:
    body: dict[str, Any] = {
        "correlation_id": cid,
        "failure_reason": "tenant is not registered",
        "handler": "node_projection_delegation",
        "original_message": {"correlation_id": cid},
    }
    if partition is not None and offset is not None:
        body.update(
            source_topic=SOURCE_TOPIC, source_partition=partition, source_offset=offset
        )
    return json.dumps(body).encode()


def _wire(cid: str, tenant: str) -> bytes:
    return json.dumps(
        {"tenant_id": tenant, "payload": {"correlation_id": cid, "task_type": "t"}}
    ).encode()


class FakeDeadLetters:
    def __init__(self, envelopes: list[bytes]) -> None:
        self.envelopes = envelopes
        self.asked: list[frozenset[str]] = []

    async def read_dead_letters(
        self, dlq_topic: str, correlation_ids: frozenset[str]
    ) -> list[bytes]:
        self.asked.append(correlation_ids)
        return self.envelopes


class FakeSource:
    """Exposes only a read: there is no commit, seek-to-group or offset reset to call."""

    def __init__(self, records: dict[tuple[str, int, int], bytes]) -> None:
        self.records = records
        self.reads: list[tuple[str, int, int]] = []

    async def read_record(
        self, topic: str, partition: int, offset: int
    ) -> bytes | None:
        self.reads.append((topic, partition, offset))
        return self.records.get((topic, partition, offset))


class FakeTenants:
    async def tenant_is_registered(self, tenant_identity: str | None) -> bool:
        return tenant_identity == REGISTERED


class FakeProjector:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any], MessageMeta]] = []
        self.raises = raises

    async def project(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        if self.raises:
            raise RuntimeError("database is down")
        self.calls.append((topic, data, meta))
        return True


def _handler(
    envelopes: list[bytes],
    records: dict[tuple[str, int, int], bytes],
    projector: FakeProjector | None = None,
) -> tuple[HandlerProjectionDlqRedrive, FakeSource, FakeProjector]:
    source = FakeSource(records)
    proj = projector or FakeProjector()
    handler = HandlerProjectionDlqRedrive(
        dead_letters=FakeDeadLetters(envelopes),
        source=source,
        tenants=FakeTenants(),
        projector=proj,
    )
    return handler, source, proj


async def test_golden_refused_record_is_reprojected_from_its_source_coordinates() -> (
    None
):
    handler, source, projector = _handler(
        [_envelope("c1", partition=2, offset=41)],
        {(SOURCE_TOPIC, 2, 41): _wire("c1", REGISTERED)},
    )

    result = await handler.handle(
        ModelProjectionDlqRedriveRequest(correlation_ids=["c1"])
    )

    assert result.verdict is EnumRedriveVerdict.REDRIVEN
    assert (result.dead_letters_read, result.reprojected, result.skipped) == (1, 1, 0)
    assert source.reads == [(SOURCE_TOPIC, 2, 41)]
    topic, data, meta = projector.calls[0]
    assert topic == SOURCE_TOPIC
    assert data["correlation_id"] == "c1"
    assert (meta.partition, meta.offset, meta.topic) == (2, 41, SOURCE_TOPIC)


async def test_golden_request_names_its_correlation_ids_to_the_reader() -> None:
    dead_letters = FakeDeadLetters([])
    handler = HandlerProjectionDlqRedrive(
        dead_letters=dead_letters,
        source=FakeSource({}),
        tenants=FakeTenants(),
        projector=FakeProjector(),
    )

    result = await handler.handle(
        ModelProjectionDlqRedriveRequest(correlation_ids=["a", "b"])
    )

    assert dead_letters.asked == [frozenset({"a", "b"})]
    assert result.verdict is EnumRedriveVerdict.NOTHING_TO_REDRIVE


async def test_error_unknown_tenant_is_skipped_and_not_projected() -> None:
    handler, _, projector = _handler(
        [_envelope("c-ok", offset=1), _envelope("c-unknown", offset=2)],
        {
            (SOURCE_TOPIC, 0, 1): _wire("c-ok", REGISTERED),
            (SOURCE_TOPIC, 0, 2): _wire("c-unknown", UNKNOWN),
        },
    )

    result = await handler.handle(ModelProjectionDlqRedriveRequest())

    by_cid = {r.correlation_id: r.outcome for r in result.records}
    assert by_cid == {
        "c-ok": EnumRedriveOutcome.REPROJECTED,
        "c-unknown": EnumRedriveOutcome.SKIPPED_TENANT_UNKNOWN,
    }
    assert [c[1]["correlation_id"] for c in projector.calls] == ["c-ok"]
    assert result.verdict is EnumRedriveVerdict.PARTIAL


async def test_error_dead_letter_without_source_coordinates_is_skipped() -> None:
    handler, source, projector = _handler(
        [_envelope("c-old", partition=None, offset=None)], {}
    )

    result = await handler.handle(ModelProjectionDlqRedriveRequest())

    assert result.records[0].outcome is EnumRedriveOutcome.SKIPPED_NO_SOURCE
    assert source.reads == []
    assert projector.calls == []


async def test_error_source_record_gone_from_the_broker_is_skipped() -> None:
    handler, _, projector = _handler([_envelope("c-gone", offset=9)], {})

    result = await handler.handle(ModelProjectionDlqRedriveRequest())

    assert result.records[0].outcome is EnumRedriveOutcome.SKIPPED_SOURCE_GONE
    assert projector.calls == []


async def test_error_projection_failure_is_reported_and_the_run_continues() -> None:
    handler, _, _ = _handler(
        [_envelope("c1", offset=1), _envelope("c2", offset=2)],
        {
            (SOURCE_TOPIC, 0, 1): _wire("c1", REGISTERED),
            (SOURCE_TOPIC, 0, 2): _wire("c2", REGISTERED),
        },
        FakeProjector(raises=True),
    )

    result = await handler.handle(ModelProjectionDlqRedriveRequest())

    assert [r.outcome for r in result.records] == [EnumRedriveOutcome.FAILED] * 2
    assert "database is down" in result.records[0].detail
    assert result.failed == 2
    assert result.reprojected == 0


async def test_error_unreadable_envelope_and_duplicate_source_do_not_stop_the_run() -> (
    None
):
    handler, source, projector = _handler(
        [b"not json", _envelope("c1", offset=1), _envelope("c1", offset=1)],
        {(SOURCE_TOPIC, 0, 1): _wire("c1", REGISTERED)},
    )

    result = await handler.handle(ModelProjectionDlqRedriveRequest())

    assert [r.outcome for r in result.records] == [
        EnumRedriveOutcome.NOT_PROJECTED,
        EnumRedriveOutcome.REPROJECTED,
        EnumRedriveOutcome.NOT_PROJECTED,
    ]
    assert len(source.reads) == 1
    assert len(projector.calls) == 1


async def test_dry_run_reads_and_resolves_but_projects_nothing() -> None:
    handler, _, projector = _handler(
        [_envelope("c1", offset=1)], {(SOURCE_TOPIC, 0, 1): _wire("c1", REGISTERED)}
    )

    result = await handler.handle(ModelProjectionDlqRedriveRequest(dry_run=True))

    assert result.verdict is EnumRedriveVerdict.DRY_RUN
    assert projector.calls == []


def test_contract_dlq_topic_is_the_delegation_writers_declared_dead_letter_topic() -> (
    None
):
    assert contract_dlq_topic() == DelegationProjectionRunner()._dlq_topics[0]


def test_dead_letter_envelope_the_writer_publishes_is_the_one_the_redrive_reads() -> (
    None
):
    import asyncio

    published: list[bytes] = []

    async def capture(topic: str, value: bytes) -> None:
        published.append(value)

    runner = DelegationProjectionRunner(publish_fn=capture)
    meta = MessageMeta(partition=5, offset=77, fallback_id="c9", topic=SOURCE_TOPIC)
    asyncio.run(runner._route_malformed_to_dlq({"correlation_id": "c9"}, "bad", meta))

    handler, source, _ = _handler(published, {})
    asyncio.run(handler.handle(ModelProjectionDlqRedriveRequest()))
    assert source.reads == [(SOURCE_TOPIC, 5, 77)]


async def test_live_source_reader_joins_no_group_and_commits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_projection_dlq_redrive_effect.handlers import (
        _kafka_readers,
    )

    seen: dict[str, Any] = {}

    class FakeConsumer:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

        async def start(self) -> None:
            return None

    fake = types.SimpleNamespace(AIOKafkaConsumer=FakeConsumer)
    monkeypatch.setitem(sys.modules, "aiokafka", fake)

    await _kafka_readers._consumer("broker:9092")

    assert seen["group_id"] is None
    assert seen["enable_auto_commit"] is False


def test_golden_contract_declares_the_completed_answer_as_its_terminal_event() -> None:
    import yaml

    from omnimarket.nodes.node_projection_dlq_redrive_effect.handlers import (
        handler_projection_dlq_redrive,
    )

    contract = yaml.safe_load(
        (handler_projection_dlq_redrive._CONTRACT).read_text(encoding="utf-8")
    )

    assert contract["runtime_dispatch"]["terminal_events"]["success"] == (
        "onex.evt.omnimarket.projection-dlq-redrive-completed.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.projection-dlq-redrive-completed.v1"
    ]
