# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The drain mark one pool host holds (OMN-24489).

A serve process subscribes to the drain topic in a drain group of its own and
replays it from the earliest offset, so a serve that restarts rebuilds the
mark. The group stays on the broker Empty until it expires -- one per start,
never one per call -- and its name stays under the lab work consumer group
prefix the host login's ACL rows cover.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from aiokafka.errors import KafkaError
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolBusMessage
from omnimarket.models.model_host_drain_command import (
    ModelHostDrainCommand,
    load_host_drain_topic,
)

logger = logging.getLogger(__name__)

Subscribe = Callable[
    [str, Callable[[Any], Awaitable[None]], str, str],
    Awaitable[Callable[[], Awaitable[None]]],
]


class HostDrainMark:
    """The newest drain command addressed to one host, read off the drain topic."""

    def __init__(self, host_name: str, topic: str | None = None) -> None:
        self._host = host_name
        self._topic = topic if topic is not None else load_host_drain_topic()
        self._command: ModelHostDrainCommand | None = None
        self._unsubscribe: Callable[[], Awaitable[None]] | None = None
        #: Set when the drain topic could not be subscribed; the host keeps
        #: serving lab work but holds no drain mark until it can.
        self.unreadable = False

    async def start(self, subscribe: Subscribe, group_prefix: str) -> None:
        group = f"{group_prefix}.drain-{uuid.uuid4().hex[:8]}"
        try:
            self._unsubscribe = await subscribe(
                self._topic, self._on_message, group, "earliest"
            )
        except (KafkaError, OSError, RuntimeError) as exc:
            logger.error(
                "lab-work drain: the drain topic %s could not be subscribed (%s); "
                "this host holds no drain mark until it can",
                self._topic,
                exc,
            )
            self.unreadable = True

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            await self._unsubscribe()
            self._unsubscribe = None

    def until(self, now: datetime) -> datetime | None:
        """The expiry of the mark while it still reads draining at ``now``."""
        if self._command is not None and self._command.reads_draining(now):
            return self._command.until
        return None

    async def _on_message(self, message: ProtocolBusMessage) -> None:
        try:
            raw = json.loads(message.value)
        except (json.JSONDecodeError, TypeError):
            logger.warning("lab-work drain: unreadable command on %s", self._topic)
            return
        payload = raw.get("payload", raw) if isinstance(raw, dict) else raw
        try:
            command = ModelHostDrainCommand.model_validate(payload)
        except ValidationError:
            logger.warning("lab-work drain: invalid command on %s", self._topic)
            return
        if command.host_name != self._host:
            return
        if self._command is None or command.issued_at > self._command.issued_at:
            self._command = command
