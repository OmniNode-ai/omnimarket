# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Postgres writer for local-identity-minted events.

It applies the same pure plan the local store applies (``plan_local_identity_write``)
inside one transaction: the registry check, the tenant setting, a lock on the
tenant, a read of its row, then at most one upsert. A written row is published
as a snapshot delta; an exact replay that writes nothing republishes the stored
row, since a publish can fail after the commit, and a later mint publishes nothing.

The tenant must be one ``tenant_registry_mirror`` records. A tenant it does not
know is refused, so the runtime dead-letters the event and nothing is stored.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_local_identity_minted import ModelLocalIdentityMinted
from omnimarket.nodes.node_projection_local_identity.handlers.handler_projection_local_identity import (
    HandlerProjectionLocalIdentity,
)
from omnimarket.nodes.node_projection_local_identity.models.model_local_identity_plan import (
    ModelLocalIdentityPlanRequest,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.envelope import strip_runner_injected_keys
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta
from omnimarket.projection.tenant_isolation import TENANT_GUC
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
    TenantRegistryResolutionError,
    async_registry_tenant_uuid,
)

_COLUMNS = ("tenant_id", "minted_at", "event_id", "updated_at")

_SELECT_ROW = """
    SELECT tenant_id, minted_at, event_id, updated_at, projection_cursor
    FROM public.local_identity
    WHERE tenant_id = $1
    FOR UPDATE
"""

_UPSERT_ROW = (
    "INSERT INTO public.local_identity (tenant_id, minted_at, event_id, updated_at) "
    "VALUES ($1, $2, $3, $4) ON CONFLICT (tenant_id) DO UPDATE SET "
    "minted_at = EXCLUDED.minted_at, event_id = EXCLUDED.event_id, updated_at = EXCLUDED.updated_at, "
    # The BIGSERIAL default fires on INSERT only; an updated row takes the next
    # cursor too, or a reader paging by cursor never sees the change.
    "projection_cursor = nextval('public.local_identity_projection_cursor_seq') "
    "RETURNING tenant_id, minted_at, event_id, updated_at, projection_cursor"
)


async def confirm_registry_tenant(conn: Any, tenant_id: str) -> str:
    """The uuid the tenant registry mirror records for this tenant, or a refusal."""
    registered = await async_registry_tenant_uuid(conn, tenant_id)
    if registered is None or str(registered) != tenant_id:
        raise TenantRegistryResolutionError(
            f"local identity refused: no row in {TENANT_REGISTRY_MIRROR_TABLE} "
            f"records tenant {tenant_id!r}; no identity row is written for it."
        )
    return tenant_id


def _parameter(name: str, value: object) -> object:
    if name in {"minted_at", "updated_at"} and isinstance(value, str):
        return datetime.fromisoformat(value)
    if name == "event_id" and isinstance(value, str):
        return uuid.UUID(value)
    return value


def _wire_row(row: dict[str, Any]) -> dict[str, Any]:
    wired: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, date | datetime):
            wired[key] = value.isoformat()
        elif isinstance(value, uuid.UUID):
            wired[key] = str(value)
        else:
            wired[key] = value
    return wired


class LocalIdentityProjectionWriter(BaseProjectionRunner):
    """Store each mint event's planned row under its tenant and publish it."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        exposures = load_projection_exposures_from_contract(
            self._contract, str(self._contract["name"]), path
        )
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (exposure for exposure in exposures if exposure.bus_backed), None
        )
        self._plan = HandlerProjectionLocalIdentity()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("dlq_topics", []))

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        publish = await self.get_publish_fn()
        if publish is None:
            raise RuntimeError(f"no DLQ publisher configured for {topic}")
        await publish(topic, value)

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        topics = self.subscribe_topics
        data = dict(input_data)
        topic = str(data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(data.pop("_partition", 0)),
            offset=int(data.pop("_offset", 0)),
            fallback_id=str(data.pop("_fallback_id", "")),
            topic=topic,
        )
        return asyncio.run(self._project_one_message(data, meta))

    async def _project_one_message(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        await self.db.connect()
        try:
            rows = await self._project_event(data, meta)
        finally:
            await self._stop_producer()
            await self.db.close()
        return {"rows_upserted": len(rows), "identity_rows": rows}

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_event(data, meta)
        return True

    async def _project_event(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> list[dict[str, Any]]:
        payload = strip_runner_injected_keys(data)
        payload.pop("_db", None)
        # ValidationError and the tenant refusal must reach the runtime's
        # POISON/DLQ classifier.
        event = ModelLocalIdentityMinted.model_validate(payload)
        tenant = str(event.tenant_id)
        written: list[dict[str, Any]] = []
        replayed: list[dict[str, Any]] = []
        async with self.db.pool.acquire() as conn, conn.transaction():
            await confirm_registry_tenant(conn, tenant)
            await conn.execute("SELECT set_config($1, $2, true)", TENANT_GUC, tenant)
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"local_identity:{tenant}"
            )
            stored = [dict(r) for r in await conn.fetch(_SELECT_ROW, tenant)]
            row = self._plan.handle(
                ModelLocalIdentityPlanRequest(
                    event=event,
                    stored_rows=tuple(stored),
                    updated_at=datetime.now(UTC),
                )
            ).row
            if row is None:
                # A publish can fail after commit: an exact replay republishes the
                # stored row without writing. A later mint republishes nothing.
                replayed = [
                    _wire_row(r)
                    for r in stored
                    if r["minted_at"] == event.minted_at.astimezone(UTC)
                ]
            else:
                returned = await conn.fetch(
                    _UPSERT_ROW, *(_parameter(name, row[name]) for name in _COLUMNS)
                )
                written.append(_wire_row(dict(returned[0])))
        if self._snapshot_exposure is not None:
            for wired in written or replayed:
                await self.publish_snapshot_delta(
                    self._snapshot_exposure,
                    op="upsert",
                    row=wired,
                    source_event_id=f"{tenant}:{event.minted_at.isoformat()}",
                    source_topic=meta.topic,
                    source_partition=meta.partition,
                    source_offset=meta.offset,
                    tenant_id=tenant,
                )
        return written


__all__ = ["LocalIdentityProjectionWriter", "confirm_registry_tenant"]
