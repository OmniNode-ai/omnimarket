# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A store-only row (set through the store, absent from the overlay home) stays live while the loader runs (OMN-26043).

Measured: a store-only row is asserted once, at set time, and the reader marks it stale after its bound, so the checks
that read it went PRECONDITION-ABSENT about six hours after each set. The loader re-asserts the rows the overlay names
in ``store_only_ids`` (injected, no row name in code): it republishes the live record with a fresh time.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.overlay_store.snapshot_root import FileOverlaySnapshot
from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.models.overlay_store import (
    EnumOverlayReadState,
    ModelOverlayReadPolicy,
    ModelOverlayStoreLoadOverlay,
    ModelOverlayStoreRecord,
)
from omnimarket.nodes.node_overlay_store_load_effect.handlers import (
    HandlerOverlayStoreLoad,
    OverlayHomeSource,
)
from omnimarket.nodes.node_overlay_store_read_effect.handlers import (
    HandlerOverlayStoreRead,
)
from tests.unit.nodes.node_overlay_store.support import (
    LOADED_TOPIC,
    NOW,
    FakeBus,
    Reporter,
    declared_topic,
    record,
    table_with,
)

pytestmark = pytest.mark.unit

SIX_HOURS = 6 * 3600
SET_AT = NOW - timedelta(hours=7)


async def _store_only_row() -> InMemoryOverlayStore:
    """A row an operator set through the store 7 hours before NOW; the overlay home holds no file for it."""
    store = InMemoryOverlayStore()
    row = record(
        table_with(cap=7), loaded_at=SET_AT, operator_paths=("hosts/[name=h1]/cap",)
    )
    assert await store.write_version(row.model_dump(mode="json"))
    return store


async def _reading_state(
    tmp_path: Path, store: InMemoryOverlayStore
) -> EnumOverlayReadState:
    reader = HandlerOverlayStoreRead(
        store=store,
        snapshot=FileOverlaySnapshot(tmp_path / "snapshot"),
        reporter=Reporter(),
        host="h1",
        policies={
            "lab_hosts": ModelOverlayReadPolicy(
                stale_after_s=SIX_HOURS, on_stale="read"
            )
        },
        clock=lambda: NOW,
    )
    return (await reader.handle("lab_hosts")).state


def _loader(
    store: InMemoryOverlayStore, bus: FakeBus, home: Path
) -> HandlerOverlayStoreLoad:
    return HandlerOverlayStoreLoad(
        source=OverlayHomeSource(home),
        bus=bus,
        topic=LOADED_TOPIC,
        loaded_by="test",
        clock=lambda: NOW,
        live=store,
        drift_topic=declared_topic("overlay-store-drift"),
        refusal_topic=declared_topic("overlay-store-refused"),
    )


async def test_known_bad_without_the_loader_the_row_is_stale_after_seven_hours(
    tmp_path: Path,
) -> None:
    store = await _store_only_row()
    assert await _reading_state(tmp_path, store) is EnumOverlayReadState.STALE


async def test_known_good_the_loader_reasserts_the_store_only_row_and_it_reads_live(
    tmp_path: Path,
) -> None:
    store = await _store_only_row()
    bus = FakeBus()
    batch = await _loader(store, bus, tmp_path / "empty-home").reassert_many(
        ["lab_hosts"]
    )
    assert [r.overlay_id for r in batch.results] == ["lab_hosts"]
    assert batch.refused == ()
    published = [v for t, _, v in bus.published if t == LOADED_TOPIC]
    assert len(published) == 1
    again = ModelOverlayStoreRecord.model_validate_json(published[0])
    before = ModelOverlayStoreRecord.model_validate(
        await store.read_current("lab_hosts")
    )
    assert again.loaded_at == NOW
    assert (again.document, again.digest, again.operator_paths) == (
        before.document,
        before.digest,
        before.operator_paths,
    )
    assert await store.write_version(again.model_dump(mode="json"))
    assert await _reading_state(tmp_path, store) is EnumOverlayReadState.FRESH


async def test_a_row_the_store_does_not_hold_is_refused_by_name_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    bus = FakeBus()
    batch = await _loader(InMemoryOverlayStore(), bus, tmp_path).reassert_many(
        ["lab_hosts"]
    )
    assert batch.results == ()
    assert [r.overlay_id for r in batch.refused] == ["lab_hosts"]
    assert [t for t, _, _ in bus.published if t == LOADED_TOPIC] == []


def test_the_overlay_names_its_store_only_rows_and_defaults_to_none() -> None:
    base = {"bus": "inmemory", "loaded_by": "x"}
    assert ModelOverlayStoreLoadOverlay.model_validate(base).store_only_ids == ()
    named = ModelOverlayStoreLoadOverlay.model_validate(
        {**base, "store_only_ids": ["a", "b"]}
    )
    assert named.store_only_ids == ("a", "b")
