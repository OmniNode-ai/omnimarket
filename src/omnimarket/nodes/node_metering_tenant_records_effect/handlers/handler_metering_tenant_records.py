# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tenant's metering records from the local evidence store (OMN-26449).

The local headline refresh and ``onex metering``'s staleness check read every row of ``delegation_events``, while the
savings view and model-routing read only the tenant's own rows, so another tenant's run, or a run with no tenant, was
counted in the headline alone. This handler is the tenant-scoped read the three callers use.

The tenant is matched as the projection writer records it: the writer stores the tenant's canonical UUID, resolving a
slug through ``tenant_registry_mirror`` or the closed legacy map, so a tenant named by slug or by a non-canonical UUID
string matches the same rows. The given string itself always matches. A row whose ``tenant_id`` is NULL is nobody's
and is never returned. A store whose ``delegation_events`` has no ``tenant_id`` column predates tenants; no row there
names any tenant, so every row is returned, as before (spec decision D1).

Read only: the store is opened with ``mode=ro`` and nothing is written.
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import UUID

from omnimarket.nodes.node_metering_summary_compute import ModelMeteringRecord
from omnimarket.nodes.node_metering_tenant_records_effect.models.model_metering_tenant_records_request import (
    ModelMeteringTenantRecordsRequest,
)
from omnimarket.projection.sqlite_metering_reader import (
    MeteringRecordsUnavailableError,
    default_metering_db_path,
    read_metering_records,
)
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
    TenantRegistryResolutionError,
    parse_tenant_uuid,
    resolve_registry_tenant_uuid,
)

_EVENTS_TABLE = "delegation_events"
_TENANT_COLUMN = "tenant_id"


def _tenant_keys(conn: sqlite3.Connection, tenant_id: str) -> frozenset[str]:
    """The stored ``tenant_id`` values that are this tenant's rows."""
    keys = {tenant_id}
    parsed = parse_tenant_uuid(tenant_id)
    if parsed is not None:
        keys.add(str(parsed))
        return frozenset(keys)
    registry_uuid: UUID | None = None
    mirror = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (TENANT_REGISTRY_MIRROR_TABLE,),
    ).fetchone()
    if mirror is not None:
        row = conn.execute(
            f"SELECT tenant_uuid FROM {TENANT_REGISTRY_MIRROR_TABLE} WHERE tenant_slug = ?",
            (tenant_id,),
        ).fetchone()
        if row is not None and row[0] is not None:
            registry_uuid = parse_tenant_uuid(str(row[0]))
    # A slug nobody resolves matches only its own string, as the writer would refuse it.
    with contextlib.suppress(TenantRegistryResolutionError):
        keys.add(
            str(resolve_registry_tenant_uuid(tenant_id, registry_uuid=registry_uuid))
        )
    return frozenset(keys)


def _owned_correlation_ids(db_path: Path, tenant_id: str) -> frozenset[str] | None:
    """The correlation ids of the tenant's rows, or None when the store has no tenant column."""
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True
        )  # no-contract-check: projection boundary
    except sqlite3.Error as exc:
        raise MeteringRecordsUnavailableError(
            f"cannot open the local delegation evidence database at {db_path}: {exc}"
        ) from exc
    try:
        columns = {
            str(info[1]) for info in conn.execute(f"PRAGMA table_info({_EVENTS_TABLE})")
        }
        if _TENANT_COLUMN not in columns:
            return None
        keys = sorted(_tenant_keys(conn, tenant_id))
        marks = ", ".join("?" for _ in keys)
        rows = conn.execute(
            f"SELECT correlation_id FROM {_EVENTS_TABLE} WHERE CAST({_TENANT_COLUMN} AS TEXT) IN ({marks})",
            keys,
        ).fetchall()
    except sqlite3.Error as exc:
        raise MeteringRecordsUnavailableError(
            f"cannot read the tenant's rows of {_EVENTS_TABLE} from {db_path}: {exc}"
        ) from exc
    finally:
        conn.close()
    return frozenset(str(row[0]) for row in rows)


class HandlerMeteringTenantRecords:
    """Read one tenant's metering records, in the order ``read_metering_records`` returns them."""

    def handle(
        self, request: ModelMeteringTenantRecordsRequest
    ) -> tuple[ModelMeteringRecord, ...]:
        db_path = (
            request.db_path
            if request.db_path is not None
            else default_metering_db_path()
        )
        records = read_metering_records(
            db_path=db_path,
            window_start=request.window_start,
            window_end=request.window_end,
            include_fixtures=request.include_fixtures,
        )
        owned = _owned_correlation_ids(db_path, request.tenant_id)
        if owned is None:
            return records
        return tuple(r for r in records if r.correlation_id in owned)


def read_tenant_records(
    tenant_id: str,
    db_path: Path | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    *,
    include_fixtures: bool = False,
) -> tuple[ModelMeteringRecord, ...]:
    """The tenant's records in ``[window_start, window_end)``, as the three callers read them."""
    return HandlerMeteringTenantRecords().handle(
        ModelMeteringTenantRecordsRequest(
            tenant_id=tenant_id,
            db_path=db_path,
            window_start=window_start,
            window_end=window_end,
            include_fixtures=include_fixtures,
        )
    )


__all__ = ["HandlerMeteringTenantRecords", "read_tenant_records"]
