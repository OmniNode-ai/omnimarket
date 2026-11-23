# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Asks the runtime's tenant provision command over the lane's bus and waits for its own answer.

The command goes out as an event envelope on the declared command topic; the answer is read from the declared completed
topic, from the log end, with no consumer group: every client sees every answer and keeps the one whose ``command_id``
is its own. The subscription is made before the publish, so an answer cannot land unheard. A bus that cannot be opened,
or no answer inside the declared wait, is ``TenantProvisionUnavailableError``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol
from uuid import uuid4

from pydantic import ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import envelope_bytes_for
from omnimarket.handlers.bus_connect.errors import LabRunBusError
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_tenant_provision_wire import (
    ModelTenantProvisionAnswer,
    ModelTenantProvisionRequest,
    ModelTenantProvisionTopics,
)
from omnimarket.nodes.node_dev_identity_provision_effect.protocols.protocol_dev_identity_transport import (
    TenantProvisionUnavailableError,
)


class ProtocolTenantBus(Protocol):
    """The two calls this client makes on the bus, in memory or on Kafka."""

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> object: ...

    async def subscribe_ungrouped(
        self, topic: str, on_message: Callable[[Any], Awaitable[None]]
    ) -> Callable[[], Awaitable[None]]: ...


class BusTenantProvisioner:
    """One tenant provision command per call, on a bus opened for the call."""

    def __init__(
        self,
        *,
        open_bus: Callable[[], AbstractAsyncContextManager[ProtocolTenantBus]],
        topics: ModelTenantProvisionTopics,
    ) -> None:
        self._open_bus = open_bus
        self._topics = topics

    async def provision(
        self, request: ModelTenantProvisionRequest
    ) -> ModelTenantProvisionAnswer:
        topics = self._topics
        loop = asyncio.get_running_loop()
        answered: asyncio.Future[ModelTenantProvisionAnswer] = loop.create_future()

        async def on_answer(message: Any) -> None:
            answer = _answer_for(message, request)
            if answer is not None and not answered.done():
                answered.set_result(answer)

        try:
            async with self._open_bus() as bus:
                unsubscribe = await bus.subscribe_ungrouped(
                    topics.completed_topic, on_answer
                )
                try:
                    await bus.publish(
                        topics.command_topic,
                        str(request.command_id).encode("utf-8"),
                        envelope_bytes_for(
                            topics.command_topic,
                            request.model_dump(mode="json"),
                            uuid4(),
                        ),
                    )
                    return await asyncio.wait_for(answered, timeout=topics.wait_seconds)
                finally:
                    with contextlib.suppress(Exception):
                        await unsubscribe()
        except TimeoutError:
            raise TenantProvisionUnavailableError(
                f"no answer on {topics.completed_topic} within {topics.wait_seconds:g}s; "
                f"is a runtime serving {topics.command_topic}?"
            ) from None
        except (LabRunBusError, OSError) as exc:
            raise TenantProvisionUnavailableError(
                f"the bus could not be opened: {type(exc).__name__}: {exc}"
            ) from None


def _answer_for(
    message: Any, request: ModelTenantProvisionRequest
) -> ModelTenantProvisionAnswer | None:
    """The answer in this message when it is for ``request``'s command id, else ``None``."""
    try:
        raw = json.loads(bytes(message.value))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    payload = raw.get("payload", raw)
    if not isinstance(payload, dict) or payload.get("command_id") != str(
        request.command_id
    ):
        return None
    try:
        return ModelTenantProvisionAnswer.model_validate(payload)
    except ValidationError:
        return None


__all__ = ["BusTenantProvisioner", "ProtocolTenantBus"]
