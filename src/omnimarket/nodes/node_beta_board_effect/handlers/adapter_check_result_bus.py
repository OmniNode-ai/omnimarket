# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The beta board's bus read of the delegation check probe's run events (OMN-26723).

The broker is the one the board node's own overlay declares (``event_bus``), resolved the way the probe's publisher resolves its own;
``KAFKA_BOOTSTRAP_SERVERS`` is never read and no credential is named here. The read assigns partitions and seeks to the first offset at or after the bound: it joins no consumer group and commits nothing.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from omnimarket.handlers.bus_binding.node_broker import (
    NodeBrokerResolutionError,
    resolve_dialable_broker,
)
from omnimarket.nodes.node_beta_board_effect.models.model_beta_board import (
    ModelCheckResultRead,
    ModelCheckResultRecord,
)
from omnimarket.topic_namespace import apply_topic_namespace

NODE = "node_beta_board_effect"
_POLL_TIMEOUT_MS = 5_000
_READ_DEADLINE_S = 30.0


def decode_record(value: bytes) -> ModelCheckResultRecord | None:
    """A run event from one message: the flat record the probe publishes, or the same record under an envelope's ``payload``."""
    try:
        doc = json.loads(value)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    inner = doc.get("payload")
    body = inner if isinstance(inner, dict) and "check_id" not in doc else doc
    try:
        return ModelCheckResultRecord.model_validate(body)
    except ValidationError:
        return None


async def _read(topic: str, since: datetime) -> list[ModelCheckResultRecord]:
    from aiokafka import AIOKafkaConsumer, TopicPartition

    broker = resolve_dialable_broker(NODE)
    options: dict[str, str] = {}
    if broker.sasl_mechanism is not None:
        options["sasl_mechanism"] = broker.sasl_mechanism
    if broker.sasl_username is not None:
        options["sasl_plain_username"] = broker.sasl_username
    if broker.sasl_password is not None:
        options["sasl_plain_password"] = broker.sasl_password.get_secret_value()
    consumer: Any = AIOKafkaConsumer(
        bootstrap_servers=broker.bootstrap_servers,
        security_protocol=broker.security_protocol or "PLAINTEXT",
        group_id=None,
        enable_auto_commit=False,
        **options,
    )
    await consumer.start()
    found: list[ModelCheckResultRecord] = []
    try:
        name = apply_topic_namespace(topic)
        await consumer.topics()
        partitions = consumer.partitions_for_topic(name)
        if not partitions:
            raise LookupError(f"topic {name} has no partitions")
        tps = [TopicPartition(name, p) for p in sorted(partitions)]
        consumer.assign(tps)
        ends = await consumer.end_offsets(tps)
        starts = await consumer.offsets_for_times(
            {tp: int(since.timestamp() * 1000) for tp in tps}
        )
        pending = set()
        for tp in tps:
            at = starts.get(tp)
            if at is None or at.offset >= ends[tp]:
                continue
            consumer.seek(tp, at.offset)
            pending.add(tp)
        while pending:
            batch = await consumer.getmany(timeout_ms=_POLL_TIMEOUT_MS)
            if not batch:
                break
            for tp, records in batch.items():
                for record in records:
                    if record.value is not None:
                        decoded = decode_record(record.value)
                        if decoded is not None:
                            found.append(decoded)
                if await consumer.position(tp) >= ends[tp]:
                    pending.discard(tp)
    finally:
        await consumer.stop()
    return found


class KafkaCheckResultSource:
    """Reads the run events the probe published on a topic since a bound, over the declared broker."""

    def read(self, topic: str, since: datetime) -> ModelCheckResultRead:
        try:
            records = asyncio.run(
                asyncio.wait_for(_read(topic, since), _READ_DEADLINE_S)
            )
        except (
            NodeBrokerResolutionError,
            KeyError,
            LookupError,
            OSError,
            TimeoutError,
            ImportError,
        ) as exc:
            return ModelCheckResultRead(
                error=f"the check result topic cannot be read: {type(exc).__name__} {exc}"[
                    :240
                ]
            )
        except Exception as exc:  # broker and client errors are the aiokafka hierarchy
            return ModelCheckResultRead(
                error=f"the check result topic cannot be read: {type(exc).__name__}"
            )
        return ModelCheckResultRead(records=tuple(records))
