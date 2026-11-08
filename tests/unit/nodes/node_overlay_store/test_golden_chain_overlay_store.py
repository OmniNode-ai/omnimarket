# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: overlay, loader, bus, store, snapshot topic, reader, snapshot file, every parser."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.container.container_inmemory import bind_infra_event_bus
from omnimarket.handlers.node_overlay_reader import find_node_overlay
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.handlers.overlay_store.snapshot_root import render_document
from omnimarket.handlers.overlay_store.store_bus_snapshot import BusSnapshotOverlayStore
from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.models.overlay_store import (
    EnumOverlayReadSource,
    EnumOverlayReadState,
    ModelOverlayStoreRecord,
)
from omnimarket.nodes.node_overlay_store_load_effect.handlers import (
    HandlerOverlayStoreLoad,
    OverlayHomeSource,
)
from omnimarket.nodes.node_overlay_store_read_effect.handlers import compare_documents
from tests.unit.nodes.node_overlay_store.support import (
    LOADED_TOPIC,
    NOW,
    SNAPSHOT_TOPIC,
    TABLE,
    Reporter,
    contract_topics,
    overlay_home,
    reader,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_the_table_travels_from_the_overlay_to_a_snapshot_equal_to_the_source(
    tmp_path: Path,
) -> None:
    bus = bind_infra_event_bus(environment="local", group="overlay-store-golden")
    await bus.start()
    store = InMemoryOverlayStore()

    # The projection's half, as the runner does it: store, read back, publish.
    async def project(message: Any) -> None:
        loaded = ModelOverlayStoreRecord.model_validate_json(message.value)
        assert await store.write_version(loaded.model_dump(mode="json"))
        current = await store.read_current(loaded.overlay_id)
        assert current is not None
        await bus.publish(
            SNAPSHOT_TOPIC,
            loaded.overlay_id.encode(),
            ModelOverlayStoreRecord.model_validate(current).model_dump_json().encode(),
        )

    await bus.subscribe(LOADED_TOPIC, on_message=project, group_id="projection")

    @contextlib.asynccontextmanager
    async def open_bus() -> AsyncIterator[Any]:
        yield bus

    host_store = BusSnapshotOverlayStore(
        open_bus=open_bus, topic=SNAPSHOT_TOPIC, group_prefix="host", window_s=0.2
    )
    reporter = Reporter()
    handler = reader(tmp_path, host_store, reporter=reporter)
    refresh = asyncio.create_task(handler.handle("lab_hosts"))
    await asyncio.sleep(0.05)
    clone = overlay_home(tmp_path)
    result = await HandlerOverlayStoreLoad(
        source=OverlayHomeSource(clone),
        bus=bus,
        topic=LOADED_TOPIC,
        loaded_by="golden",
        clock=lambda: NOW,
    ).handle("lab_hosts")
    reading = await refresh
    await bus.close()

    assert reading.state is EnumOverlayReadState.FRESH
    assert reading.source is EnumOverlayReadSource.STORE
    assert reading.store_error is None
    assert reading.digest == result.digest
    assert reporter.readings == [reading]

    root = tmp_path / "snapshot"
    written = root / "lab_hosts" / "overlay.yaml"
    source = yaml.safe_load(
        (clone / "overlays" / "lab_hosts" / "overlay.yaml").read_text()
    )
    compared = compare_documents(source, yaml.safe_load(written.read_text()))
    assert compared.equal, compared.differing
    assert compared.rows_compared == len(TABLE["hosts"])

    # Every reader takes the snapshot without importing this node: the overlay root layout...
    assert (
        find_node_overlay("lab_hosts", {"ONEX_SKILL_OVERLAY_ROOTS": str(root)})
        == written
    )
    # ...and the file a table reader is pointed at, through the typed table.
    table = resolve_lab_hosts_table({"ONEX_LAB_RUN_HOSTS": str(written)})
    assert [host.name for host in table.hosts] == ["h1", "h2"]
    assert table.hosts[0].ledger_path == "$OMNI_HOME/docs/ledger.md"
    assert table.hosts[1].available is False


def test_deployed_chain_travels_the_topics_its_three_nodes_declare() -> None:
    """node_overlay_store_load_effect publishes what node_projection_overlay_store subscribes to, and
    node_projection_overlay_store publishes the snapshot node_overlay_store_read_effect reads."""
    assert LOADED_TOPIC == "onex.evt.omnimarket.overlay-store-loaded.v1"
    assert contract_topics("node_projection_overlay_store", "subscribe_topics") == [
        LOADED_TOPIC
    ]
    # onex-topic-allow: a projection snapshot topic has the projection's own name form.
    assert SNAPSHOT_TOPIC == "onex.snapshot.projection.overlay-store.v1"
    assert contract_topics("node_overlay_store_read_effect", "publish_topics") == [
        "onex.evt.omnimarket.overlay-store-read.v1"
    ]
    assert contract_topics("node_overlay_store_read_effect", "subscribe_topics") == []


@pytest.mark.asyncio
async def test_a_second_load_of_the_same_document_moves_the_time_not_the_digest() -> (
    None
):
    store = InMemoryOverlayStore()
    first = {"overlay_id": "a", "loaded_at": "2026-11-06T01:00:00+00:00", "digest": "d"}
    later = {"overlay_id": "a", "loaded_at": "2026-11-06T02:00:00+00:00", "digest": "d"}
    assert await store.write_version(first)
    assert await store.write_version(later)
    assert not await store.write_version(first)
    assert await store.read_current("a") == later


def test_the_rendered_file_keeps_key_order_and_folds_no_line() -> None:
    rendered = render_document(TABLE).decode()
    row = next(line for line in rendered.splitlines() if "unavailable_reason" in line)
    assert "\"its owner's x: " in row
    assert row.rstrip().endswith('colon, "')
    assert rendered.index("hosts:") < rendered.index("unit_needs:")
    assert rendered.index("  target:") < rendered.index("  clones:")
    assert list(yaml.safe_load(rendered)) == list(TABLE)
