# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain for the cloud-side hook-event ledger (OMN-17201, leg 5).

Chain under test, end to end with no Kafka and no Postgres:

    verbatim hook body
        -> ModelEventEnvelope (the canonical cloud wire model)
        -> resolve_physical_topic (THE single runtime topic resolver)
        -> unwrap_envelope (the shared projection decode the runner uses)
        -> HandlerHookLedgerProjection.project_event
        -> a public.hook_events row

PROVENANCE OF THE FIXTURES, STATED PRECISELY -- this is the half that matters,
because the sibling leg above this one (OMN-17919) has a green unit suite and a
live consumer that rejected 261 of 261 real records, for exactly the reason a
loose claim here would reproduce.

* The hook BODIES below are verbatim wire bytes captured from the four live
  omniclaude hook topics on the stability-lane broker
  (``omnibase-infra-stability-test-redpanda``) on 2026-08-29/30. They are the
  same bytes ``tests/unit/nodes/test_golden_chain_work_events.py`` uses, so the
  local L1 ledger and this cloud ledger are proven against ONE corpus rather
  than two divergent sets of invented fixtures.
* The cloud ENVELOPE around them is NOT captured, and is not claimed to be. No
  hook event has crossed to the cloud bus yet -- OMN-17919 (the lane mirror
  rejects every record) and OMN-17382 (the outbound leg is wedged on one
  foreign-tenant record) both stand open. So the envelope is CONSTRUCTED HERE
  BY THE PLATFORM'S OWN CANONICAL MODELS -- ``ModelEventEnvelope`` and
  ``resolve_physical_topic`` -- rather than hand-written to match this handler.
  If either changes shape, this test moves with it. That is weaker than
  captured bytes and is recorded as such; it is not presented as a live proof.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.nodes.node_bus_forwarder_effect.services.service_gateway_topic_transform import (
    resolve_physical_topic,
)

from omnimarket.nodes.node_projection_hook_ledger.handlers.handler_hook_ledger_projection import (
    TABLE,
    HandlerHookLedgerProjection,
)
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.runner import MessageMeta

TENANT_SLUG = "beta-gateway-canary"
_SESSION = "5bc4e084-6a53-4f69-936e-998985adbcf5"

#: Verbatim bytes off the stability-lane broker, 2026-08-29/30.
_WIRE: list[tuple[str, str]] = [
    (
        "onex.evt.omniclaude.session-started.v1",
        '{"session_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"working_directory": "omni_home", "hook_source": "startup", '
        '"correlation_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"causation_id": null, "emitted_at": "2026-08-29T22:18:47.100000+00:00", '
        '"entity_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"schema_version": "1.0.0"}',
    ),
    (
        "onex.evt.omniclaude.prompt-submitted.v1",
        '{"session_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"working_directory": "omni_home", "prompt_length": 412, '
        '"hook_source": "user_prompt_submit", '
        '"correlation_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"causation_id": null, "emitted_at": "2026-08-29T22:19:02.500000+00:00", '
        '"entity_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"schema_version": "1.0.0"}',
    ),
    (
        "onex.evt.omniclaude.tool-executed.v1",
        '{"session_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"working_directory": "omni_home", "tool_name": "Bash", '
        '"duration_ms": 184, "interrupted": false, '
        '"hook_source": "post_tool_use", '
        '"correlation_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"causation_id": null, "emitted_at": "2026-08-30T01:59:07.891697+00:00", '
        '"entity_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"schema_version": "1.0.0"}',
    ),
    (
        "onex.evt.omniclaude.session-ended.v1",
        '{"session_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"reason": "clear", '
        '"correlation_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"causation_id": null, "emitted_at": "2026-08-30T02:10:00.000000+00:00", '
        '"entity_id": "5bc4e084-6a53-4f69-936e-998985adbcf5", '
        '"schema_version": "1.0.0"}',
    ),
]


class _RecordingDb:
    """Captures the parameters bound to each INSERT."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(
        self, sql: str, *args: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return [{"ok": True}]


def _cloud_bytes(canonical_topic: str, body: str) -> bytes:
    """Wrap a captured hook body the way the cloud outbound leg encodes it."""
    envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
        event_type=canonical_topic,
        payload=json.loads(body),
    )
    raw = json.loads(envelope.model_dump_json(exclude_none=True))
    # The forwarder stamps its trust-boundary tags on every outbound publish
    # (service_gateway_forwarder._prepare_outbound). The tenant tag is the one
    # this ledger cross-checks the wire topic against.
    raw.setdefault("metadata", {}).setdefault("tags", {}).update(
        {
            "gateway_tenant_slug": TENANT_SLUG,
            "gateway_direction": "local-to-cloud",
            "gateway_canonical_topic": canonical_topic,
        }
    )
    return json.dumps(raw).encode("utf-8")


def _runner() -> tuple[HandlerHookLedgerProjection, _RecordingDb]:
    runner = HandlerHookLedgerProjection.__new__(HandlerHookLedgerProjection)
    runner._load_contract()
    db = _RecordingDb()
    runner._db = db  # type: ignore[assignment]

    async def _publish(topic: str, value: bytes) -> None:
        return None

    runner._publish_fn = _publish  # type: ignore[assignment]
    return runner, db


def _project_all() -> tuple[_RecordingDb, list[str]]:
    runner, db = _runner()
    wire_topics: list[str] = []
    for offset, (canonical, body) in enumerate(_WIRE):
        wire_topic = resolve_physical_topic(canonical, tenant_slug=TENANT_SLUG)
        wire_topics.append(wire_topic)
        data = unwrap_envelope(_cloud_bytes(canonical, body))
        assert data is not None, "the shared decode must accept the cloud shape"
        meta = MessageMeta(partition=0, offset=offset, fallback_id="", topic=wire_topic)
        assert asyncio.run(runner.project_event(wire_topic, data, meta)) is True
    return db, wire_topics


@pytest.mark.unit
def test_every_captured_hook_class_traverses_the_chain_to_a_row() -> None:
    db, _topics = _project_all()
    assert len(db.calls) == len(_WIRE)
    for sql, _args in db.calls:
        assert TABLE in sql


@pytest.mark.unit
def test_each_row_binds_the_canonical_topic_not_the_tenant_prefixed_wire_topic() -> (
    None
):
    """event_type is the CLASS. Storing the wire topic would fork the class per tenant."""
    db, _topics = _project_all()
    bound_types = [args[2] for _sql, args in db.calls]
    assert bound_types == [canonical for canonical, _ in _WIRE]


@pytest.mark.unit
def test_each_row_binds_the_wire_topic_tenant_as_its_tenant_id() -> None:
    db, _topics = _project_all()
    assert {args[0] for _sql, args in db.calls} == {TENANT_SLUG}


@pytest.mark.unit
def test_the_correlation_id_the_ac3_probe_searches_for_reaches_the_row() -> None:
    """AC3 reads the cloud row back BY CORRELATION ID. This is that column."""
    db, _topics = _project_all()
    assert {args[6] for _sql, args in db.calls} == {_SESSION}


@pytest.mark.unit
def test_occurred_at_is_bound_as_a_real_datetime_not_an_isoformat_string() -> None:
    """OMN-15909's exact defect: a str bound to TIMESTAMPTZ passes a mock and
    raises asyncpg.DataError against a real column."""
    import datetime as dt

    db, _topics = _project_all()
    for _sql, args in db.calls:
        assert isinstance(args[3], dt.datetime), args[3]


@pytest.mark.unit
def test_four_distinct_hook_classes_derive_four_distinct_content_addresses() -> None:
    """If they collided, the table's UNIQUE key would collapse a session to one row."""
    db, _topics = _project_all()
    shas = [args[1] for _sql, args in db.calls]
    assert len(set(shas)) == len(_WIRE)


@pytest.mark.unit
def test_replaying_the_whole_captured_corpus_derives_byte_identical_addresses() -> None:
    """Replay must be idempotent, and it must not depend on the delivery offsets."""
    first, _t1 = _project_all()
    second, _t2 = _project_all()
    assert [a[1] for _s, a in first.calls] == [a[1] for _s, a in second.calls]


@pytest.mark.unit
def test_the_stored_payload_is_the_captured_body_verbatim() -> None:
    db, _topics = _project_all()
    for (canonical, body), (_sql, args) in zip(_WIRE, db.calls, strict=True):
        assert json.loads(args[4]) == json.loads(body), canonical
