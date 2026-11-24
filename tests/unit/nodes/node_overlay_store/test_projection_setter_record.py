# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay-store projection stores the record a setting set publishes, ``operator_paths`` included (it refused it before)."""

from __future__ import annotations

import types
from datetime import timedelta

import pytest

from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.models.overlay_store import ModelOverlaySettingAudit
from omnimarket.nodes.node_projection_overlay_store.handlers import (
    handler_overlay_store_projection as handler_module,
)
from omnimarket.nodes.node_projection_overlay_store.handlers.handler_overlay_store_projection import (
    HandlerOverlayStoreProjectionRunner,
)
from tests.unit.nodes.node_overlay_store.support import LOADED_TOPIC, NOW, record

pytestmark = pytest.mark.unit

SET_PATH = "floor_fill_live/pool_snapshot_path"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryOverlayStore:
    held = InMemoryOverlayStore()
    monkeypatch.setattr(handler_module, "PostgresOverlayStore", lambda _pool: held)
    monkeypatch.setattr(
        HandlerOverlayStoreProjectionRunner,
        "db_for",
        lambda _self, _table, **_options: types.SimpleNamespace(pool=None),
    )
    return held


@pytest.mark.asyncio
async def test_projection_stores_a_setting_record_with_operator_paths_and_its_audit_row(
    store: InMemoryOverlayStore,
) -> None:
    """A set publishes the loader's record plus ``operator_paths`` with its write id, revision and audit row; the projection
    stores it with its paths and publishes the audit row, the snapshot and the reply."""
    published: list[tuple[str, bytes | None, bytes]] = []

    async def publish(topic: str, key: bytes | None, value: bytes) -> None:
        published.append((topic, key, value))

    runner = HandlerOverlayStoreProjectionRunner(publish_keyed_fn=publish)
    payload = record(loaded_at=NOW + timedelta(minutes=1)).model_dump(mode="json")
    payload["operator_paths"] = [SET_PATH]
    payload["write_id"] = "w-setter"
    payload["based_on_revision"] = 0
    payload["audit"] = ModelOverlaySettingAudit(
        overlay_id="lab_hosts",
        key="floor_fill_live.pool_snapshot_path",
        setting_path=SET_PATH,
        actor="setter",
        previous_present=False,
        previous_value=None,
        new_value="p",
        document_digest=payload["digest"],
        set_at=NOW + timedelta(minutes=1),
    ).model_dump(mode="json")
    assert await runner.project_event(LOADED_TOPIC, payload, None) is True
    current = await store.read_current("lab_hosts")
    assert current is not None
    assert current["digest"] == payload["digest"]
    assert current["operator_paths"] == [SET_PATH]
    assert len(published) == 3


@pytest.mark.asyncio
async def test_projection_refuses_operator_paths_without_an_audit_row(
    store: InMemoryOverlayStore,
) -> None:
    """The interim projection stored such a record and dropped its paths; the paths now come with their audit row or not at all."""
    published: list[tuple[str, bytes | None, bytes]] = []

    async def publish(topic: str, key: bytes | None, value: bytes) -> None:
        published.append((topic, key, value))

    runner = HandlerOverlayStoreProjectionRunner(publish_keyed_fn=publish)
    payload = record(loaded_at=NOW + timedelta(minutes=1)).model_dump(mode="json")
    payload["operator_paths"] = [SET_PATH]
    with pytest.raises(ValueError, match="without an audit row"):
        await runner.project_event(LOADED_TOPIC, payload, None)
    assert await store.read_current("lab_hosts") is None
    assert published == []


@pytest.mark.asyncio
async def test_projection_still_refuses_an_unknown_field(
    store: InMemoryOverlayStore,
) -> None:
    """Positive control: only ``operator_paths`` is accepted beside the record; any other extra field is still refused."""
    runner = HandlerOverlayStoreProjectionRunner(publish_keyed_fn=None)
    payload = record(loaded_at=NOW).model_dump(mode="json")
    payload["unexpected"] = 1
    with pytest.raises(ValueError, match="unexpected"):
        await runner.project_event(LOADED_TOPIC, payload, None)
