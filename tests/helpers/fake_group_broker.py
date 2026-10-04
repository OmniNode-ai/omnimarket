# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A broker that remembers consumer groups the way Kafka does.

A group appears when a consumer subscribes with it, stays behind (``Empty``)
when the consumer leaves, and goes away only when an admin client deletes it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from typing import Any

import pytest

from omnimarket.lab_work import bus as lab_work_bus


class FakeGroupBroker:
    def __init__(self) -> None:
        self.groups: set[str] = set()
        self.live: set[str] = set()
        self.subscribers: dict[str, list[Callable[[Any], Awaitable[None]]]] = {}
        self.published: list[tuple[str, bytes]] = []
        self.on_publish: Callable[[str, bytes], Awaitable[None]] | None = None
        self.bootstrap_servers = "fake-broker:9092"

    @property
    def empty_groups(self) -> set[str]:
        return self.groups - self.live

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> None:
        self.published.append((topic, value))
        if self.on_publish is not None:
            await self.on_publish(topic, value)

    async def deliver(self, topic: str, value: bytes) -> None:
        class _Message:
            def __init__(self, raw: bytes) -> None:
                self.value = raw

        for callback in list(self.subscribers.get(topic, [])):
            await callback(_Message(value))

    async def subscribe(
        self,
        topic: str,
        node_identity: Any = None,
        on_message: Callable[[Any], Awaitable[None]] | None = None,
        *,
        group_id: str | None = None,
        **_: object,
    ) -> Callable[[], Awaitable[None]]:
        assert group_id is not None
        assert on_message is not None
        self.groups.add(group_id)
        self.live.add(group_id)
        self.subscribers.setdefault(topic, []).append(on_message)

        async def unsubscribe() -> None:
            self.subscribers[topic].remove(on_message)
            self.live.discard(group_id)

        return unsubscribe


class FakeAdmin:
    def __init__(self, broker: FakeGroupBroker) -> None:
        self._broker = broker

    def delete_consumer_groups(self, group_ids: list[str]) -> dict[str, Future[None]]:
        out: dict[str, Future[None]] = {}
        for group_id in group_ids:
            future: Future[None] = Future()
            if group_id in self._broker.live:
                future.set_exception(RuntimeError("GROUP_NOT_EMPTY"))
            else:
                self._broker.groups.discard(group_id)
                future.set_result(None)
            out[group_id] = future
        return out


def install_fake_admin(
    monkeypatch: pytest.MonkeyPatch, broker: FakeGroupBroker
) -> None:
    monkeypatch.setattr(
        lab_work_bus, "_new_admin", lambda _bootstrap: FakeAdmin(broker)
    )
