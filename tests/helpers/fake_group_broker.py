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
        self.active: dict[tuple[str, str], str] = {}
        self.bootstrap_servers = "fake-broker:9092"

    def get_consumer_groups(self) -> dict[tuple[str, str], str]:
        return dict(self.active)

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
        # Like the Kafka bus, one broker group per (topic, subscription group).
        effective = f"{group_id}.__t.{topic}"
        self.groups.add(effective)
        self.live.add(effective)
        self.active[(topic, group_id)] = effective
        self.subscribers.setdefault(topic, []).append(on_message)

        async def unsubscribe() -> None:
            self.subscribers[topic].remove(on_message)
            self.live.discard(effective)
            self.active.pop((topic, group_id), None)

        return unsubscribe


class FakeAdmin:
    """``refuse_first`` stands for the static-membership session timeout: the
    broker refuses a group that left but whose member has not yet expired."""

    def __init__(self, broker: FakeGroupBroker, refuse_first: int = 0) -> None:
        self._broker = broker
        self._refuse_first = refuse_first
        self.attempts: dict[str, int] = {}

    def delete_consumer_groups(self, group_ids: list[str]) -> dict[str, Future[None]]:
        out: dict[str, Future[None]] = {}
        for group_id in group_ids:
            future: Future[None] = Future()
            self.attempts[group_id] = self.attempts.get(group_id, 0) + 1
            if (
                group_id in self._broker.live
                or self.attempts[group_id] <= self._refuse_first
            ):
                future.set_exception(RuntimeError("NON_EMPTY_GROUP"))
            else:
                self._broker.groups.discard(group_id)
                future.set_result(None)
            out[group_id] = future
        return out


def install_fake_admin(
    monkeypatch: pytest.MonkeyPatch, broker: FakeGroupBroker, refuse_first: int = 0
) -> FakeAdmin:
    admin = FakeAdmin(broker, refuse_first)
    monkeypatch.setattr(lab_work_bus, "_new_admin", lambda _bootstrap: admin)
    monkeypatch.setattr(lab_work_bus, "_RETRY_DELAY_SECONDS", 0.0)
    return admin
