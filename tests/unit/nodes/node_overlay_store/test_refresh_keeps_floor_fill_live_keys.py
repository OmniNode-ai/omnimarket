# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The six ``floor_fill_live`` keys an operator set survive a loader publish, through the real set, projection and loader (OMN-26495).

The chain is the one the dev lane runs: the loader publishes the overlay home's document, the set command reads the
live record and publishes a record with the six keys marked as operator-set, the projection stores that record and
publishes the snapshot, and the next loader run reads the snapshot and publishes again. Only the broker and the
database are replaced (a routing bus, an in-memory store); every handler is the real one. A loader that replaces the
whole document, or a projection that drops the operator paths, leaves the six keys absent after the publish.
"""

from __future__ import annotations

import copy
import json
import types
from collections.abc import Mapping
from datetime import timedelta
from importlib import resources
from typing import Any, cast

import pytest
import yaml
from pydantic import BaseModel, JsonValue

from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    SEAM_KEYS,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_overlay_document import (
    ModelFloorFillOverlayDocument,
)
from omnimarket.nodes.node_overlay_setting_set_effect.handlers.handler_overlay_setting_set import (
    HandlerOverlaySettingSet,
    load_settable_settings,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_request import (
    ModelOverlaySettingSetRequest,
)
from omnimarket.nodes.node_overlay_store_load_effect.handlers import (
    HandlerOverlayStoreLoad,
)
from omnimarket.nodes.node_projection_overlay_store.handlers import (
    handler_overlay_store_projection as projection_module,
)
from omnimarket.nodes.node_projection_overlay_store.handlers.handler_overlay_store_projection import (
    HandlerOverlayStoreProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from tests.unit.nodes.node_overlay_store.support import (
    LOADED_TOPIC,
    NOW,
    declared_topic,
)

pytestmark = pytest.mark.unit

OVERLAY = "node_floor_fill_orchestrator"
# Synthetic values: the test sets the keys and reads the same values back; no deployment value is named here.
SET_VALUES: dict[str, Any] = {
    "pool_snapshot_path": "/srv/example/set/pool-snapshot.json",
    "pool_record_suffix": ".set-read-at.json",
    "pool_snapshot_max_age_seconds": 240,
    "pool_engine": "set-engine",
    "mint_timeout_seconds": 20.0,
    "andon_state_dir": "/srv/example/set/exception-queue",
}
REPOSITORY_VALUES: dict[str, Any] = {
    "pool_snapshot_path": "/srv/example/repository/pool-snapshot.json",
    "pool_record_suffix": ".repository-read-at.json",
    "pool_snapshot_max_age_seconds": 180,
    "pool_engine": "repository-engine",
    "mint_timeout_seconds": 10.0,
    "andon_state_dir": "/srv/example/repository/exception-queue",
}


class _RoutingBus:
    """A broker that hands the loaded topic to the projection and keeps every other topic for the test."""

    def __init__(self, runner: HandlerOverlayStoreProjectionRunner) -> None:
        self._runner = runner
        self.other: list[tuple[str, bytes]] = []

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> None:
        if topic == LOADED_TOPIC:
            await self._runner.project_event(
                topic,
                json.loads(value),
                MessageMeta(topic=topic, partition=0, offset=0, fallback_id="f"),
            )
        else:
            self.other.append((topic, value))


def _example_document(*, keys: Mapping[str, Any] | None) -> dict[str, Any]:
    """The floor fill example document; the six seam keys are those of ``keys``, or absent when it is None."""
    example = (
        resources.files("omnimarket.nodes.node_floor_fill_orchestrator")
        .joinpath("overlay.example.yaml")
        .read_text(encoding="utf-8")
    )
    document: dict[str, Any] = yaml.safe_load(example)
    live = document["floor_fill_live"]
    for key in SEAM_KEYS:
        live.pop(key, None)
    if keys is not None:
        live.update(keys)
    return document


class _Source:
    """The overlay home as the loader reads it: one document and the model that validates it."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        self._document = dict(document)

    def read(self, overlay_id: str) -> tuple[dict[str, JsonValue], str]:
        assert overlay_id == OVERLAY
        return copy.deepcopy(self._document), f"{OVERLAY}@test"

    def model_of(self, overlay_id: str) -> type[BaseModel]:
        return ModelFloorFillOverlayDocument


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> tuple[InMemoryOverlayStore, _RoutingBus]:
    store = InMemoryOverlayStore()
    monkeypatch.setattr(projection_module, "PostgresOverlayStore", lambda _pool: store)
    monkeypatch.setattr(
        HandlerOverlayStoreProjectionRunner,
        "db_for",
        lambda _self, _table, **_options: types.SimpleNamespace(pool=None),
    )
    return store, _RoutingBus(HandlerOverlayStoreProjectionRunner())


async def _load(
    home: _Source,
    store: InMemoryOverlayStore,
    bus: _RoutingBus,
    *,
    at_minutes: int,
) -> None:
    await HandlerOverlayStoreLoad(
        source=home,
        bus=bus,
        topic=LOADED_TOPIC,
        loaded_by="overlay-loader",
        clock=lambda: NOW + timedelta(minutes=at_minutes),
        live=store,
        drift_topic=declared_topic("overlay-store-drift"),
        refusal_topic=declared_topic("overlay-store-refused"),
    ).handle(OVERLAY)


async def _set(
    store: InMemoryOverlayStore, bus: _RoutingBus, keys: Mapping[str, Any]
) -> list[str]:
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_overlay_setting_set_effect")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    (audit_topic,) = [
        topic
        for topic in contract["event_bus"]["publish_topics"]
        if ".overlay-setting-set." in topic
    ]
    setter = HandlerOverlaySettingSet(
        reader=store,
        bus=bus,
        settings=load_settable_settings(contract),
        loaded_topic=LOADED_TOPIC,
        audit_topic=audit_topic,
        max_live_age_s=7200,
        clock=lambda: NOW + timedelta(minutes=5),
    )
    for key, value in keys.items():
        await setter.handle(
            ModelOverlaySettingSetRequest(
                overlay_id=OVERLAY,
                key=f"floor_fill_live.{key}",
                value=value,
                actor="test-operator",
            )
        )
    return [topic for topic, _ in bus.other]


async def _live_keys(store: InMemoryOverlayStore) -> dict[str, Any]:
    current = await store.read_current(OVERLAY)
    assert current is not None
    document = cast("dict[str, Any]", current["document"])
    live: dict[str, Any] = document["floor_fill_live"]
    return {key: live.get(key) for key in SEAM_KEYS}


@pytest.mark.asyncio
async def test_six_floor_fill_live_keys_survive_a_loader_publish(
    wired: tuple[InMemoryOverlayStore, _RoutingBus],
) -> None:
    store, bus = wired
    # The overlay home carries none of the six keys, as the overlay home's main document did when the keys were wiped.
    home = _Source(_example_document(keys=None))
    await _load(home, store, bus, at_minutes=0)
    assert set((await _live_keys(store)).values()) == {None}
    audit_topics = await _set(store, bus, SET_VALUES)
    assert len(audit_topics) == len(SEAM_KEYS)  # one audit row per set key
    assert await _live_keys(store) == SET_VALUES
    # The loader publishes again, later than every set: a replace-the-document loader wins the store here.
    await _load(home, store, bus, at_minutes=60)
    assert await _live_keys(store) == SET_VALUES


@pytest.mark.asyncio
async def test_an_unset_key_takes_the_overlay_default_beside_kept_keys(
    wired: tuple[InMemoryOverlayStore, _RoutingBus],
) -> None:
    store, bus = wired
    await _load(_Source(_example_document(keys=None)), store, bus, at_minutes=0)
    kept = {key: value for key, value in SET_VALUES.items() if key != "pool_engine"}
    await _set(store, bus, kept)
    # The overlay home now carries a value for every key, the key nobody set included.
    await _load(
        _Source(_example_document(keys=REPOSITORY_VALUES)),
        store,
        bus,
        at_minutes=60,
    )
    assert await _live_keys(store) == {
        **kept,
        "pool_engine": REPOSITORY_VALUES["pool_engine"],
    }
