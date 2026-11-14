"""OMN-22811 AC1: a refused projection record's dead-letter envelope names its source.

The delegation writer's dead-letter topic held 36,190 records whose envelopes
carried no source topic, partition or offset, so nothing could read the refused
record again. Each refusal path below is a recorded shape from that topic: a
malformed event, a refusal inside a typed projection, and the runner's poison
path that tenant-unresolved events reach. The envelope has to carry the
coordinates the consumer read the record from.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.models.model_message_meta import MessageMeta
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.dlq import build_dlq_envelope
from tests.helpers.tenant_registry import mock_tenant_registry

DELEGATION_DLQ_TOPIC = "onex.dlq.omnimarket.projection-delegation-malformed.v1"


def _runner() -> tuple[DelegationProjectionRunner, list[tuple[str, bytes]]]:
    published: list[tuple[str, bytes]] = []

    async def capture(topic: str, value: bytes) -> None:
        published.append((topic, value))

    runner = DelegationProjectionRunner(publish_fn=capture)
    db = MagicMock(spec=AsyncpgAdapter)
    db.execute = AsyncMock(return_value=None)
    mock_tenant_registry(db)
    runner._db = db
    return runner, published


def _only_envelope(published: list[tuple[str, bytes]]) -> dict[str, Any]:
    rows = [value for topic, value in published if topic == DELEGATION_DLQ_TOPIC]
    assert len(rows) == 1
    envelope: dict[str, Any] = json.loads(rows[0].decode("utf-8"))
    return envelope


def test_malformed_event_envelope_carries_source_coordinates() -> None:
    runner, published = _runner()
    topic = runner._topic_delegate_skill_completed
    meta = MessageMeta(partition=3, offset=4242, fallback_id="corr-1", topic=topic)

    asyncio.run(runner.project_event(topic, {"correlation_id": "corr-1"}, meta))

    envelope = _only_envelope(published)
    assert envelope["source_topic"] == topic
    assert envelope["source_partition"] == 3
    assert envelope["source_offset"] == 4242
    assert envelope["correlation_id"] == "corr-1"


def test_missing_fields_refusal_carries_source_coordinates() -> None:
    runner, published = _runner()
    topic = runner._topic_delegated
    meta = MessageMeta(partition=1, offset=77, fallback_id="corr-2", topic=topic)

    asyncio.run(runner.project_event(topic, {"correlation_id": "corr-2"}, meta))

    envelope = _only_envelope(published)
    assert (
        envelope["source_topic"],
        envelope["source_partition"],
        envelope["source_offset"],
    ) == (topic, 1, 77)


def test_runner_poison_path_envelope_carries_source_coordinates() -> None:
    runner, published = _runner()
    topic = runner._topic_delegate_skill_completed
    meta = MessageMeta(partition=2, offset=9001, fallback_id="corr-3", topic=topic)
    refusal = RuntimeError("tenant 'h101' is not registered")

    routed = asyncio.run(
        runner._route_poison_to_dlq(topic, {"correlation_id": "corr-3"}, refusal, meta)
    )

    assert routed is True
    envelope = _only_envelope(published)
    assert envelope["source_topic"] == topic
    assert envelope["source_partition"] == 2
    assert envelope["source_offset"] == 9001
    assert envelope["correlation_id"] == "corr-3"


def test_envelope_without_known_source_names_no_coordinates() -> None:
    envelope = build_dlq_envelope(
        original_message={"correlation_id": "c"},
        failure_reason="refused",
        handler="h",
        correlation_id="c",
    )
    assert "source_topic" not in envelope
    assert "source_partition" not in envelope
    assert "source_offset" not in envelope
