# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A loader run fills the settings that have no live value and keeps a value an operator set (OMN-26495)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.models.overlay_store import ModelOverlayStoreRecord, document_digest
from omnimarket.nodes.node_overlay_store_load_effect.handlers import (
    HandlerOverlayStoreLoad,
    OverlayHomeSource,
)
from tests.unit.nodes.node_overlay_store.support import (
    LOADED_TOPIC,
    NOW,
    TABLE,
    FakeBus,
    declared_topic,
    overlay_home,
    record,
    table_with,
)

pytestmark = pytest.mark.unit

CAP = "hosts/[name=h1]/cap"
OPERATOR_CAP = 4


async def _load(
    tmp_path: Path, repository: dict[str, Any], live: InMemoryOverlayStore
) -> tuple[ModelOverlayStoreRecord, FakeBus]:
    bus = FakeBus()
    home = tmp_path / f"run{len(list(tmp_path.iterdir()))}"
    home.mkdir()
    await HandlerOverlayStoreLoad(
        source=OverlayHomeSource(overlay_home(home, repository)),
        bus=bus,
        topic=LOADED_TOPIC,
        loaded_by="test",
        clock=lambda: NOW,
        live=live,
        drift_topic=declared_topic("overlay-store-drift"),
        refusal_topic=declared_topic("overlay-store-refused"),
    ).handle("lab_hosts")
    loaded = [value for topic, _, value in bus.published if topic == LOADED_TOPIC]
    assert len(loaded) == 1
    return ModelOverlayStoreRecord.model_validate_json(loaded[0]), bus


async def _live(table: dict[str, Any], *paths: str) -> InMemoryOverlayStore:
    store = InMemoryOverlayStore()
    assert await store.write_version(
        record(table, operator_paths=paths).model_dump(mode="json")
    )
    return store


@pytest.mark.asyncio
async def test_a_value_an_operator_set_survives_a_loader_run(tmp_path: Path) -> None:
    live = await _live(table_with(cap=OPERATOR_CAP), CAP)
    stored, _ = await _load(tmp_path, TABLE, live)
    assert stored.document["hosts"][0]["cap"] == OPERATOR_CAP  # type: ignore[index]
    assert stored.operator_paths == (CAP,)
    assert TABLE["hosts"][0]["cap"] != OPERATOR_CAP
    assert stored.digest == document_digest(stored.document)


@pytest.mark.asyncio
async def test_fills_empty_settings_and_replaces_those_no_operator_set(
    tmp_path: Path,
) -> None:
    held = table_with(cap=OPERATOR_CAP)
    del held["registry_root_candidates"]  # a setting with no live value
    held["hosts"][1]["cap"] = 99  # a live value nobody set through the store
    live = await _live(held, CAP)
    repository = copy.deepcopy(TABLE)
    repository["hosts"][1]["cap"] = 5
    stored, _ = await _load(tmp_path, repository, live)
    expected = copy.deepcopy(repository)
    expected["hosts"][0]["cap"] = OPERATOR_CAP
    assert stored.document == expected
    assert stored.document["registry_root_candidates"] == ["$HOME/registry"]
    assert stored.document["hosts"][1]["cap"] == 5  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["absent", "no_operator_paths"])
async def test_no_live_values_stores_the_document_exactly_as_before(
    tmp_path: Path, held: str
) -> None:
    live = InMemoryOverlayStore()
    if held == "no_operator_paths":
        assert await live.write_version(
            record(table_with(cap=OPERATOR_CAP)).model_dump(mode="json")
        )
    stored, bus = await _load(tmp_path, TABLE, live)
    assert stored.document == TABLE
    assert stored.digest == document_digest(TABLE)
    assert stored.operator_paths == ()
    # The published payload is the record of before this change, down to its keys.
    payload = json.loads(bus.published[0][2])
    assert sorted(payload) == [
        "digest",
        "document",
        "loaded_at",
        "loaded_by",
        "overlay_id",
        "source_ref",
    ]
    assert [topic for topic, _, _ in bus.published] == [LOADED_TOPIC]


@pytest.mark.asyncio
async def test_cleared_value_refills_from_the_repository_on_the_next_run(
    tmp_path: Path,
) -> None:
    store = await _live(table_with(cap=OPERATOR_CAP), CAP)
    first, _ = await _load(tmp_path, TABLE, store)
    assert first.document["hosts"][0]["cap"] == OPERATOR_CAP  # type: ignore[index]
    # The store command clears the operator-set value: the record keeps the value and drops the path.
    assert await store.write_version(
        first.model_copy(
            update={"operator_paths": (), "loaded_at": NOW.replace(hour=2)}
        ).model_dump(mode="json")
    )
    second, _ = await _load(tmp_path, TABLE, store)
    assert second.document == TABLE
    assert second.operator_paths == ()


@pytest.mark.asyncio
async def test_an_operator_value_for_a_setting_the_repository_dropped_stays_live(
    tmp_path: Path,
) -> None:
    path = "hosts/[name=h1]/fallback_proxy_jump"
    live = await _live(table_with(fallback_proxy_jump="operator-jump"), path)
    repository = copy.deepcopy(TABLE)
    del repository["hosts"][0]["fallback_proxy_jump"]
    stored, _ = await _load(tmp_path, repository, live)
    assert stored.document["hosts"][0]["fallback_proxy_jump"] == "operator-jump"  # type: ignore[index]
    assert stored.operator_paths == (path,)
