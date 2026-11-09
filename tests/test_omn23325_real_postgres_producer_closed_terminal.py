# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23325: real-Postgres write path with the producer stopped between deliveries.

The runtime host stops the Kafka producer after every delivery. Against a real,
migrated Postgres schema (the ``INTEGRATION_POSTGRES_*`` database, as in
``tests/test_omn15909_real_postgres_projection_write_path_gate.py``), every
delivery must still land its row AND emit its terminal event; before the fix
the second and later deliveries landed the row and lost the event
(``ProducerClosed``). Skips without a reachable database, like its sibling.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from aiokafka.errors import ProducerClosed
from pydantic import SecretStr

from omnimarket.projection.runner import MessageMeta, ModelProjectionRuntimeBinding
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
    _real_delegation_completed_payload,
)


class _FakeProducer:
    instances: list[_FakeProducer] = []

    def __init__(self, **_kwargs: Any) -> None:
        self.started = False
        self.sent: list[tuple[str, bytes]] = []
        _FakeProducer.instances.append(self)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def send_and_wait(self, topic: str, value: bytes, **_kwargs: Any) -> None:
        if not self.started:
            raise ProducerClosed()
        self.sent.append((topic, value))


@pytest.mark.integration
class TestTerminalEventsSurviveProducerStopOnRealPostgres:
    async def test_every_delivery_lands_its_row_and_its_terminal_event(self) -> None:
        _FakeProducer.instances = []
        with patch("omnimarket.projection.runner.AIOKafkaProducer", _FakeProducer):
            async with _provisioned_runner() as (runner, admin_conn, _schema):
                runner._runtime_binding = ModelProjectionRuntimeBinding(
                    kafka_bootstrap_servers="localhost:9092",
                    database_url=SecretStr("postgresql://stub/stub"),
                )
                ids = [str(uuid4()) for _ in range(3)]
                for n, correlation_id in enumerate(ids):
                    data = _real_delegation_completed_payload(
                        correlation_id=correlation_id,
                        tenant_id="beta-business-proof",
                    )
                    meta = MessageMeta(
                        partition=0, offset=n, fallback_id=correlation_id
                    )
                    ok = await runner.project_event(
                        runner._topic_delegation_completed, data, meta
                    )
                    assert ok is True
                    # What KeptPools.project does after every runtime delivery.
                    await runner._stop_producer()

                rows = await admin_conn.fetch(
                    "SELECT correlation_id FROM delegation_events "
                    "WHERE correlation_id = ANY($1::text[])",
                    ids,
                )
        assert len(rows) == 3
        sent = [m for p in _FakeProducer.instances for m in p.sent]
        # The real writer also publishes snapshot deltas through the same
        # producer; only the terminal events are this test's subject.
        terminal_topic = runner._terminal_topic
        assert terminal_topic is not None
        assert [t for t, _ in sent].count(terminal_topic) == 3
