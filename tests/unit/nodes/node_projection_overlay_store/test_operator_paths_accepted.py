# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection accepts the record a set sends: it names ``operator_paths`` and the table holds them.

Written against the projection's runner as it stood before the store kept provenance: that record model refused the
field (``extra="forbid"``), so no set reached the store.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.nodes.node_projection_overlay_store.handlers import (
    handler_overlay_store_projection as handler_module,
)
from omnimarket.nodes.node_projection_overlay_store.handlers.handler_overlay_store_projection import (
    HandlerOverlayStoreProjectionRunner,
)
from tests.unit.nodes.node_projection_overlay_store.support import (
    LOADED_TOPIC,
    META,
    OVERLAY_ID,
    PATH_OWNER,
    PROJECTION_NODE,
    REPLY_TOPIC,
    contract,
    default_load,
    set_write,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_the_projection_accepts_a_set_record_that_names_operator_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = InMemoryOverlayStore()
    monkeypatch.setattr(handler_module, "PostgresOverlayStore", lambda _pool: held)
    monkeypatch.setattr(
        HandlerOverlayStoreProjectionRunner,
        "db_for",
        lambda _self, _table, **_options: types.SimpleNamespace(pool=None),
    )
    published: list[tuple[str, bytes | None, bytes]] = []

    async def publish(topic: str, key: bytes | None, value: bytes) -> None:
        published.append((topic, key, value))

    runner = HandlerOverlayStoreProjectionRunner(publish_keyed_fn=publish)
    await runner.project_event(LOADED_TOPIC, default_load(), META)
    write: dict[str, Any] = set_write(based_on_revision=1)
    assert write["operator_paths"] == [PATH_OWNER]
    assert await runner.project_event(LOADED_TOPIC, write, META)
    current = await held.read_current(OVERLAY_ID)
    assert current is not None
    assert current["operator_paths"] == [PATH_OWNER]
    assert current["document"]["owner"] == "owner-b"


def test_write_reply_topic_is_declared_and_published() -> None:
    assert REPLY_TOPIC == "onex.evt.omnimarket.overlay-store-write-reply.v1"
    declared = contract(PROJECTION_NODE)["event_bus"]["publish_topics"]
    assert "onex.evt.omnimarket.overlay-store-write-reply.v1" in declared
