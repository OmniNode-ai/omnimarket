# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22478: one overlay of a real overlay home through a real broker and a real Postgres.

Loader, bus, the projection runner, the store, the snapshot topic, the reader, the snapshot file: equal
to the source row by row at the end. It runs where a lab host names a clone of the overlay home and either
holds a bus login (``OVERLAY_STORE_LIVE_LANE``) or names a broker (``OVERLAY_STORE_LIVE_BOOTSTRAP``). It SKIPS
without one of those, ``OVERLAY_STORE_LIVE_CLONE`` and ``INTEGRATION_POSTGRES_PASSWORD``.
``OVERLAY_STORE_LIVE_IDS`` names the overlays, comma separated.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import yaml

from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.handlers.overlay_store.snapshot_root import FileOverlaySnapshot
from omnimarket.handlers.overlay_store.store_bus_snapshot import BusSnapshotOverlayStore
from omnimarket.models.overlay_store import (
    EnumOverlayReadSource,
    EnumOverlayReadState,
    ModelOverlayReadPolicy,
)
from omnimarket.nodes.node_overlay_store_load_effect.handlers import (
    HandlerOverlayStoreLoad,
    OverlayHomeSource,
)
from omnimarket.nodes.node_overlay_store_read_effect.handlers import (
    HandlerOverlayStoreRead,
    compare_documents,
)
from omnimarket.projection.runner import MessageMeta
from tests.test_overlay_store_real_postgres import MIGRATION, _ConnectionRunner, _dsn
from tests.unit.nodes.node_overlay_store.support import LOADED_TOPIC, SNAPSHOT_TOPIC

pytestmark = [pytest.mark.integration, pytest.mark.kafka]


class _NoReporter:
    async def report(self, reading: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_a_real_overlay_travels_a_real_broker_and_database_and_reads_back_equal(
    tmp_path: Path,
) -> None:
    lane = os.environ.get("OVERLAY_STORE_LIVE_LANE")
    clone = os.environ.get("OVERLAY_STORE_LIVE_CLONE")
    bootstrap = os.environ.get("OVERLAY_STORE_LIVE_BOOTSTRAP")
    if not ((lane or bootstrap) and clone):
        pytest.skip("no live lane or broker, or no overlay home clone, named")
    if not os.environ.get("INTEGRATION_POSTGRES_PASSWORD"):
        pytest.skip("no database named")
    ids = os.environ.get("OVERLAY_STORE_LIVE_IDS", "lab_hosts").split(",")
    root = workspace_root(os.environ)

    admin = await asyncpg.connect(_dsn())
    database = f"overlay_store_{uuid.uuid4().hex[:12]}"
    await admin.execute(f"CREATE DATABASE {database}")
    connection = await asyncpg.connect(_dsn(database))
    await connection.execute("CREATE SCHEMA IF NOT EXISTS omninode_internal")
    await connection.execute(MIGRATION.read_text(encoding="utf-8"))

    def open_bus() -> Any:
        return open_lane_bus(
            bus="kafka",
            lane=None if bootstrap else lane,
            kafka_bootstrap=bootstrap,
            root=root,
        )

    try:
        async with open_bus() as bus:

            async def publish(topic: str, value: bytes) -> None:
                await bus.publish(topic, None, value)

            runner = _ConnectionRunner(connection, publish)
            done: asyncio.Queue[str] = asyncio.Queue()

            async def project(message: Any) -> None:
                data = json.loads(bytes(message.value).decode("utf-8"))
                await runner.project_event(
                    LOADED_TOPIC,
                    data,
                    MessageMeta(
                        topic=LOADED_TOPIC, partition=0, offset=0, fallback_id="f"
                    ),
                )
                await done.put(data["overlay_id"])

            stop = await bus.subscribe(
                LOADED_TOPIC,
                on_message=project,
                group_id=f"overlay-store-live-{uuid.uuid4().hex}",
                auto_offset_reset="latest",
            )
            await asyncio.sleep(
                10
            )  # the consumer joins its group before anything is published
            loader = HandlerOverlayStoreLoad(
                source=OverlayHomeSource(Path(clone)),
                bus=bus,
                topic=LOADED_TOPIC,
                loaded_by="overlay-store-live-chain",
            )
            results = [await loader.handle(overlay_id) for overlay_id in ids]
            for _ in ids:
                await asyncio.wait_for(done.get(), timeout=60)
            await stop()

        snapshot = FileOverlaySnapshot(tmp_path / "snapshot")
        reader = HandlerOverlayStoreRead(
            store=BusSnapshotOverlayStore(
                open_bus=open_bus,
                topic=SNAPSHOT_TOPIC,
                group_prefix="overlay-store-live",
                window_s=20,
            ),
            snapshot=snapshot,
            reporter=_NoReporter(),
            host="live-chain",
            policies={
                overlay_id: ModelOverlayReadPolicy(stale_after_s=3600, on_stale="read")
                for overlay_id in ids
            },
        )
        for overlay_id, result in zip(ids, results, strict=True):
            reading = await reader.handle(overlay_id)
            assert reading.state is EnumOverlayReadState.FRESH, reading
            assert reading.source is EnumOverlayReadSource.STORE, reading
            assert reading.digest == result.digest
            source = yaml.safe_load(
                (Path(clone) / "overlays" / overlay_id / "overlay.yaml").read_text()
            )
            stored = json.loads(
                await connection.fetchval(
                    "SELECT document FROM omninode_internal.overlay_store "
                    "WHERE overlay_id = $1 ORDER BY last_loaded_at DESC LIMIT 1",
                    overlay_id,
                )
            )
            from_store = compare_documents(source, stored)
            from_snapshot = compare_documents(
                source, snapshot.read_document(overlay_id)
            )
            assert from_store.equal, from_store.differing
            assert from_snapshot.equal, from_snapshot.differing
            print(
                f"{overlay_id} digest={result.digest} rows={from_store.rows_compared} "
                f"paths={from_store.paths_compared} source_ref={result.source_ref} equal"
            )
    finally:
        await connection.close()
        await admin.execute(f"DROP DATABASE {database}")
        await admin.close()
