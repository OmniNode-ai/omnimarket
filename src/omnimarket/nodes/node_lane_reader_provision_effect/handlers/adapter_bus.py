# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind the provisioning handler to the command and completed topics the contract declares."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

import yaml

from ..models.model_lane_reader_provision import ModelLaneReaderProvisionRequest
from .handler_lane_reader_provision import HandlerLaneReaderProvision

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


class _Message(Protocol):
    @property
    def value(self) -> bytes: ...


class _Bus(Protocol):
    async def publish(self, topic: str, key: bytes | None, value: bytes) -> object: ...

    async def subscribe(
        self,
        topic: str,
        *,
        on_message: Callable[[_Message], Awaitable[None]],
        group_id: str,
    ) -> Callable[[], Awaitable[None]]: ...


def contract_topics() -> tuple[str, str]:
    """The command topic the node consumes and the completed topic it publishes, read from its contract."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    return (
        contract["event_bus"]["subscribe_topics"][0],
        contract["event_bus"]["publish_topics"][0],
    )


async def serve_on_bus(
    bus: _Bus, handler: HandlerLaneReaderProvision, *, group_id: str
) -> Callable[[], Awaitable[None]]:
    """Consume provisioning requests from the bus and publish one completed event per request."""
    command_topic, completed_topic = contract_topics()

    async def on_message(message: _Message) -> None:
        request = ModelLaneReaderProvisionRequest.model_validate_json(message.value)
        result = handler.handle(request)
        await bus.publish(
            completed_topic,
            request.host.encode(),
            result.model_dump_json().encode(),
        )

    return await bus.subscribe(command_topic, on_message=on_message, group_id=group_id)
