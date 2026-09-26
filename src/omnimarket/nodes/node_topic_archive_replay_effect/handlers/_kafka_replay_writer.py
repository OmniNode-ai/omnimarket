# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The replay effect's broker write: publishes replayed records.

This node's contract declares the Kafka transport and the replay topic.
Broker auth comes from the shared omnibase_infra builder over the lane's
standard KAFKA_* environment, so no credential is read or named here.
"""

from __future__ import annotations

import os
from typing import Any

#: The broker address for runtime dispatch, read when first used.
BOOTSTRAP_ENV = "KAFKA_BOOTSTRAP_SERVERS"


class AiokafkaReplayWriter:
    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap = bootstrap_servers
        self._producer: Any = None

    async def publish(
        self,
        topic: str,
        *,
        key: bytes | None,
        value: bytes | None,
        headers: list[tuple[str, bytes | None]],
        timestamp_ms: int,
    ) -> None:
        if self._producer is None:
            from aiokafka import AIOKafkaProducer
            from omnibase_infra.event_bus.kafka_auth import (
                build_aiokafka_auth_kwargs_from_env,
            )

            self._producer = AIOKafkaProducer(
                bootstrap_servers=self._bootstrap,
                acks="all",
                **build_aiokafka_auth_kwargs_from_env(),
            )
            await self._producer.start()
        await self._producer.send_and_wait(
            topic,
            key=key,
            value=value,
            headers=[(k, v if v is not None else b"") for k, v in headers],
            timestamp_ms=timestamp_ms,
        )

    async def close(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None


class LazyKafkaReplayWriter(AiokafkaReplayWriter):
    """Resolves the broker address from the environment on first use."""

    def __init__(self) -> None:
        super().__init__("")

    async def publish(
        self,
        topic: str,
        *,
        key: bytes | None,
        value: bytes | None,
        headers: list[tuple[str, bytes | None]],
        timestamp_ms: int,
    ) -> None:
        if not self._bootstrap:
            self._bootstrap = os.environ[BOOTSTRAP_ENV]
        await super().publish(
            topic, key=key, value=value, headers=headers, timestamp_ms=timestamp_ms
        )
