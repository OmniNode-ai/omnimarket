# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The done-items projection writer (OMN-25104).

Each work-ledger TERMINAL message (v1 event body or v2 inbound envelope) carries
the ledger row it came from. The writer re-parses that row, asks the pure fold
``node_done_items_compute`` which tickets it makes done, and upserts one row per
done ticket into ``done_items``. A held row is replaced only by a newer done
event (by done_at, then done_row_id), so a replay or an out-of-order delivery
leaves the same rows. A TERMINAL that is not done (handed-off, for one) writes
nothing, and a merge never reaches this writer at all: merged is not done.
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
from pydantic import ValidationError

from omnimarket.models.done_items import ModelDoneItem, ModelDoneItemsRequest
from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.models.model_work_ledger_projection_inbound import (
    ModelWorkLedgerProjectionInbound,
)
from omnimarket.nodes.node_done_items_compute.handlers import HandlerDoneItems
from omnimarket.nodes.node_projection_done_items.ports.done_items_store import (
    PostgresDoneItemsStore,
    ProtocolDoneItemsStore,
    SqliteDoneItemsStore,
    StoreValue,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    parse_ledger_row,
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
TABLE = "done_items"
COLUMNS = ("ticket", "done_at", "done_row_id", "lane", "pr", "closes", "projected_at")
_V2_ROW_SCHEMA = "work-ledger-event/2"

_T = TypeVar("_T")


def upsert_statement(relation: str) -> str:
    """Insert, or replace a held row only with a newer done event."""
    placeholders = ", ".join(f"${i}" for i in range(1, len(COLUMNS) + 1))
    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c != "ticket")
    return (
        f"INSERT INTO {relation} ({', '.join(COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT (ticket) DO UPDATE SET {assignments} "
        f"WHERE ({TABLE}.done_at, {TABLE}.done_row_id) "
        "< (EXCLUDED.done_at, EXCLUDED.done_row_id) "
        "RETURNING ticket"
    )


def _row(item: ModelDoneItem, projected_at: datetime) -> dict[str, StoreValue]:
    return {
        "ticket": item.ticket,
        "done_at": item.done_at,
        "done_row_id": item.done_row_id,
        "lane": item.lane,
        "pr": item.pr,
        "closes": item.closes,
        "projected_at": projected_at,
    }


def done_items_from_payload(payload: dict[str, Any]) -> tuple[ModelDoneItem, ...]:
    """The done items one TERMINAL message makes, from the ledger row it carries.

    A payload with no ledger row, or a row the ledger parser refuses, raises a
    pydantic ValidationError: the one failure both omnimarket's consume loop
    (POISON) and omnibase_infra's in-process projection callback (a content
    failure) send to the contract's DLQ with the offset advanced. A plain
    ValueError would be retried forever by the in-process callback.
    """
    try:
        return _done_items_from_payload(payload)
    except ValidationError:
        raise
    except ValueError as exc:
        raise ValidationError.from_exception_data(
            "WorkLedgerTerminalPayload",
            [
                {
                    "type": "value_error",
                    "loc": ("raw_row",),
                    "input": payload.get("raw_row"),
                    "ctx": {"error": str(exc)},
                }
            ],
        ) from exc


def _done_items_from_payload(payload: dict[str, Any]) -> tuple[ModelDoneItem, ...]:
    if payload.get("row_schema") == _V2_ROW_SCHEMA:
        inbound = ModelWorkLedgerProjectionInbound.model_validate(payload)
        event = parse_ledger_row(
            inbound.raw_row, ledger_id=inbound.ledger_id, source=inbound.source
        )
    else:
        raw_row = payload.get("raw_row")
        if not isinstance(raw_row, str) or not raw_row:
            raise ValueError("a work-ledger TERMINAL payload carries no raw_row")
        options = {
            key: value
            for key in ("ledger_id", "source")
            if isinstance(value := payload.get(key), str) and value
        }
        event = parse_ledger_row(raw_row, **options)
    return HandlerDoneItems().handle(ModelDoneItemsRequest(events=(event,))).items


class DoneItemsProjectionWriter(BaseProjectionRunner):
    """Persists the done items each TERMINAL message makes and publishes the changes."""

    #: The shared runtime dispatches this writer once per message in process.
    onex_runtime_inprocess_dispatch = True

    def __init__(
        self,
        contract_path: Path | None = None,
        *,
        store: ProtocolDoneItemsStore | None = None,
    ) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        exposures = load_projection_exposures_from_contract(
            self._contract,
            str(self._contract.get("name", "projection_done_items")),
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
            raise RuntimeError(f"no publisher available for done-items DLQ {topic}")
        await publish(topic, value)

    def _store(self) -> ProtocolDoneItemsStore:
        if self._injected_store is not None:
            return self._injected_store
        binding = self._runtime_binding
        if binding is not None:
            url = binding.resolve_database_url()
            if urlsplit(url).scheme.lower() in SQLITE_SCHEMES:
                return SqliteDoneItemsStore(sqlite_path_from_dsn(url))
        return PostgresDoneItemsStore(self.db)

    def project_request(self, request: ModelProjectionRequest) -> dict[str, Any]:
        """RuntimeLocal shim: one injected message, one fold, one write."""
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
        items = done_items_from_payload(data)
        store = self._store()
        await store.connect()
        try:
            return await self._project(store, items, meta)
        finally:
            await self._stop_producer()
            await store.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone entrypoint: read the payload, connect, project, report consumed.

        The payload is read before the store is opened, so a message that is not
        a ledger row goes to the contract's DLQ whether or not the database answers.
        """
        items = done_items_from_payload(data)
        store = self._store()
        await store.connect()
        try:
            await self._project(store, items, meta)
        finally:
            await store.close()
        return True

    async def _project(
        self,
        store: ProtocolDoneItemsStore,
        items: tuple[ModelDoneItem, ...],
        meta: MessageMeta,
    ) -> dict[str, Any]:
        relation = (
            TABLE if isinstance(store, SqliteDoneItemsStore) else f"{SCHEMA}.{TABLE}"
        )
        projected_at = datetime.now(UTC)
        values = {item.ticket: _row(item, projected_at) for item in items}
        # Every row of one event in one transaction: a ticket written while
        # another of the same event failed would leave the Done count wrong.
        returned = await store.apply_rows(
            upsert_statement(relation),
            [tuple(row[c] for c in COLUMNS) for row in values.values()],
        )
        changed = {str(row["ticket"]) for row in returned}
        written: list[dict[str, Any]] = []
        # Published only after the event's rows committed.
        for ticket, row in values.items():
            if ticket not in changed:
                continue
            served = {**row, "projected_at": projected_at.isoformat()}
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
        return {"rows_upserted": len(written), "done_items_rows": written}


__all__ = [
    "COLUMNS",
    "DoneItemsProjectionWriter",
    "done_items_from_payload",
    "upsert_statement",
]
