# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor integration rows projection writer (OMN-25103).

Each hourly integration pass reaches this writer on
``onex.evt.omnimarket.floor-integration-pass.v1`` as the floor projection
envelope ``node_integration_reading_publish_effect`` publishes: its payload is
the whole ``ModelIntegrationProjection`` (one reading per merged change, keyed
``repository#number``, with the pass's counts). The table holds the newest pass:
every row of it is upserted, carrying the pass's counts and ``as_of``, and rows
of an older pass that the newest no longer names are deleted. A pass older than
the one held writes nothing, so a replay or an out-of-order delivery leaves the
same rows. The dashboard's Running figure is the number of rows whose ``state``
is ``running``, which equals the ``seen_running`` every row carries.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

import yaml

from omnimarket.models.floor_projection import ModelFloorProjectionEnvelope
from omnimarket.models.integration_reading import (
    ModelIntegrationProjection,
    ModelIntegrationReading,
)
from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.nodes.node_projection_floor_integration.ports.floor_integration_store import (
    ProtocolFloorIntegrationStore,
    SqliteFloorIntegrationStore,
    StoreValue,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.handler_shim import RUNTIME_INJECTED_KEYS
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    deterministic_correlation_id,
)
from omnimarket.projection.sqlite_database import (
    SQLITE_SCHEMES,
    sqlite_path_from_dsn,
)

SCHEMA = "omninode_internal"
TABLE = "floor_integration_rows"
COLUMNS = (
    "key",
    "repository",
    "number",
    "title",
    "merged_at",
    "target",
    "state",
    "gap_class",
    "step",
    "running_path",
    "running_line",
    "merged",
    "seen_running",
    "settled_elsewhere",
    "reading_owed",
    "unintegrated",
    "as_of",
    "projected_at",
)

_T = TypeVar("_T")


def _iso(moment: datetime) -> str:
    """One text form for every timestamp, so text order is time order."""
    return moment.astimezone(UTC).isoformat()


def upsert_statement(relation: str) -> str:
    """Insert, or replace a held row only from a pass at least as new."""
    placeholders = ", ".join(f"${i}" for i in range(1, len(COLUMNS) + 1))
    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c != "key")
    return (
        f"INSERT INTO {relation} ({', '.join(COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT (key) DO UPDATE SET {assignments} "
        f"WHERE {TABLE}.as_of <= EXCLUDED.as_of "
        "RETURNING key"
    )


def held_as_of_statement(relation: str) -> str:
    """The newest pass the table holds."""
    return f"SELECT MAX(as_of) AS as_of FROM {relation}"


def prune_statement(relation: str) -> str:
    """Delete the rows of older passes that the newest pass no longer names."""
    return f"DELETE FROM {relation} WHERE as_of < $1 RETURNING key"


def _row(
    key: str,
    reading: ModelIntegrationReading,
    projection: ModelIntegrationProjection,
    projected_at: datetime,
) -> dict[str, StoreValue]:
    counts = projection.counts
    return {
        "key": key,
        "repository": reading.repository,
        "number": reading.number,
        "title": reading.title,
        "merged_at": _iso(reading.merged_at),
        "target": reading.target,
        "state": reading.state.value,
        "gap_class": reading.gap_class.value,
        "step": reading.step,
        "running_path": reading.running_path,
        "running_line": reading.running_line,
        "merged": counts.merged,
        "seen_running": counts.seen_running,
        "settled_elsewhere": counts.settled_elsewhere,
        "reading_owed": counts.reading_owed,
        "unintegrated": counts.unintegrated,
        "as_of": _iso(projection.as_of),
        "projected_at": projected_at,
    }


def projection_from_payload(payload: dict[str, Any]) -> ModelIntegrationProjection:
    """The integration pass one floor projection envelope carries."""
    envelope = ModelFloorProjectionEnvelope.model_validate(payload)
    return ModelIntegrationProjection.model_validate(envelope.payload)


class FloorIntegrationProjectionWriter(BaseProjectionRunner):
    """Holds the newest integration pass's rows and publishes the changes."""

    #: The shared runtime dispatches this writer once per message in process.
    onex_runtime_inprocess_dispatch = True

    def __init__(
        self,
        contract_path: Path | None = None,
        *,
        store: ProtocolFloorIntegrationStore | None = None,
    ) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        exposures = load_projection_exposures_from_contract(
            self._contract,
            str(self._contract.get("name", "projection_floor_integration")),
            path,
        )
        self._exposure: ProjectionTableConfig | None = next(
            (e for e in exposures if e.bus_backed), None
        )
        self._injected_store = store
        self._dispatch_lock = threading.Lock()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        """Read the quarantine topic from the contract, never restate it here."""
        return list(self._contract.get("event_bus", {}).get("dlq_topics", []))

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        publish = await self.get_publish_fn()
        if publish is None:
            raise RuntimeError(
                f"no publisher available for floor integration DLQ {topic}"
            )
        await publish(topic, value)

    def _store(self) -> ProtocolFloorIntegrationStore:
        if self._injected_store is not None:
            return self._injected_store
        binding = self._runtime_binding
        if binding is not None:
            url = binding.resolve_database_url()
            if urlsplit(url).scheme.lower() in SQLITE_SCHEMES:
                return SqliteFloorIntegrationStore(sqlite_path_from_dsn(url))
        return self.db

    def project_request(self, request: ModelProjectionRequest) -> dict[str, Any]:
        """RuntimeLocal shim: one injected message, one pass, one write."""
        topics = self.subscribe_topics
        topic = request.topic_or(topics[0] if topics else "")
        partition = request.partition
        offset = request.offset
        fallback_id = request.fallback_id or (
            deterministic_correlation_id(topic, partition, offset)
        )
        payload = {
            k: v
            for k, v in dict(request.payload).items()
            if k not in RUNTIME_INJECTED_KEYS and k != "_fallback_id"
        }
        meta = MessageMeta(
            partition=partition, offset=offset, fallback_id=fallback_id, topic=topic
        )
        with self._dispatch_lock:
            return self._run(self._project_one_message(payload, meta))

    @staticmethod
    def _run(coro: Coroutine[Any, Any, _T]) -> _T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    async def _project_one_message(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        store = self._store()
        await store.connect()
        try:
            return await self._project(store, data, meta)
        finally:
            await self._stop_producer()
            await store.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone entrypoint: connect, project, report consumed."""
        store = self._store()
        await store.connect()
        try:
            await self._project(store, data, meta)
        finally:
            await store.close()
        return True

    async def _project(
        self,
        store: ProtocolFloorIntegrationStore,
        data: dict[str, Any],
        meta: MessageMeta,
    ) -> dict[str, Any]:
        projection = projection_from_payload(data)
        relation = (
            TABLE
            if isinstance(store, SqliteFloorIntegrationStore)
            else f"{SCHEMA}.{TABLE}"
        )
        incoming = _iso(projection.as_of)
        held = await store.execute(held_as_of_statement(relation))
        held_as_of = held[0].get("as_of") if held else None
        if isinstance(held_as_of, datetime):
            held_as_of = _iso(held_as_of)
        if isinstance(held_as_of, str) and held_as_of > incoming:
            return {"rows_upserted": 0, "rows_deleted": 0, "skipped_older_pass": True}

        statement = upsert_statement(relation)
        projected_at = datetime.now(UTC)
        written: list[dict[str, Any]] = []
        for key, reading in projection.rows.items():
            values = _row(key, reading, projection, projected_at)
            if not await store.execute(statement, *(values[c] for c in COLUMNS)):
                continue
            served = {**values, "projected_at": projected_at.isoformat()}
            written.append(served)
            if self._exposure is not None:
                await self.publish_snapshot_delta(
                    self._exposure,
                    op="upsert",
                    row=served,
                    source_event_id=meta.fallback_id,
                    source_topic=meta.topic,
                    source_partition=meta.partition,
                    source_offset=meta.offset,
                )
        deleted = await store.execute(prune_statement(relation), incoming)
        for gone in deleted:
            if self._exposure is not None:
                await self.publish_snapshot_delta(
                    self._exposure,
                    op="delete",
                    row=None,
                    key={"key": gone["key"]},
                    source_event_id=meta.fallback_id,
                    source_topic=meta.topic,
                    source_partition=meta.partition,
                    source_offset=meta.offset,
                )
        return {
            "rows_upserted": len(written),
            "rows_deleted": len(deleted),
            "floor_integration_rows": written,
        }


__all__ = [
    "COLUMNS",
    "FloorIntegrationProjectionWriter",
    "held_as_of_statement",
    "projection_from_payload",
    "prune_statement",
    "upsert_statement",
]
