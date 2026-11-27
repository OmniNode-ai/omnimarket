# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task 20: the generalized writer applies every snapshot record to the injected store, newest revision winning."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.overlay_store.store_valkey import ValkeyOverlayStore
from omnimarket.models.overlay_store.model_overlay_settings_record import (
    ModelOverlaySettingsRecord,
)
from omnimarket.nodes.node_host_table_keys_ingest_effect.handlers.handler_overlay_snapshot_write import (
    HandlerOverlaySnapshotWrite,
)

T0 = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
NODE = Path("src/omnimarket/nodes/node_host_table_keys_ingest_effect")
SNAPSHOT_TOPIC = "onex.snapshot.projection.overlay-store.v1"


class _FakeValkey:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, name: str) -> str | None:
        return self.data.get(name)

    async def set(self, name: str, value: str) -> object:
        self.data[name] = value
        return True


def _record(
    overlay_id: str, revision: int | None, beat: int = 0
) -> ModelOverlaySettingsRecord:
    payload: dict[str, Any] = {
        "overlay_id": overlay_id,
        "document": {"v": revision},
        "digest": "d" * 64,
    }
    if revision is not None:
        payload["revision"] = revision
        payload["heartbeat_at"] = (T0 + timedelta(seconds=beat)).isoformat()
    return ModelOverlaySettingsRecord.model_validate(payload)


@pytest.mark.asyncio
async def test_every_overlay_is_applied_under_the_prefix() -> None:
    client = _FakeValkey()
    handler = HandlerOverlaySnapshotWrite(
        store=ValkeyOverlayStore(client, key_prefix="p")
    )
    await handler.handle(_record("lab_hosts", 3))
    await handler.handle(_record("node_floor_fill_orchestrator", 1))
    assert sorted(client.data) == ["p:lab_hosts", "p:node_floor_fill_orchestrator"]
    assert json.loads(client.data["p:lab_hosts"])["revision"] == 3


@pytest.mark.asyncio
async def test_a_replayed_older_record_never_moves_a_value_back() -> None:
    client = _FakeValkey()
    handler = HandlerOverlaySnapshotWrite(
        store=ValkeyOverlayStore(client, key_prefix="p")
    )
    await handler.handle(_record("lab_hosts", 5))
    await handler.handle(_record("lab_hosts", 4))
    assert json.loads(client.data["p:lab_hosts"])["revision"] == 5


@pytest.mark.asyncio
async def test_a_pre_revision_record_is_skipped_and_counted() -> None:
    client = _FakeValkey()
    store = ValkeyOverlayStore(client, key_prefix="p")
    handler = HandlerOverlaySnapshotWrite(store=store)
    await handler.handle(_record("lab_hosts", None))
    assert client.data == {}
    assert store.reading().skipped_pre_revision == 1


def test_the_handler_reaches_its_store_only_through_the_constructor() -> None:
    source = (NODE / "handlers/handler_overlay_snapshot_write.py").read_text()
    imported = [
        n.module or ""
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.ImportFrom)
    ]
    assert not any("store_valkey" in m or "backend_" in m for m in imported)
    with pytest.raises(TypeError):
        HandlerOverlaySnapshotWrite()  # type: ignore[call-arg]


def test_the_contract_declares_the_compacted_snapshot_topic_as_a_direct_read() -> None:
    # A snapshot topic name carries no message category, so the dispatcher can route nothing on it:
    # it is declared as a direct read, never as a subscription or a route.
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["snapshot_read"]["topic"] == SNAPSHOT_TOPIC
    assert SNAPSHOT_TOPIC not in contract["event_bus"]["subscribe_topics"]
    routed = {h.get("topic") for h in contract["handler_routing"]["handlers"]}
    assert SNAPSHOT_TOPIC not in routed
