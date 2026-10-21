# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Postgres writer for bus-backed local environment reports.

It applies the same pure plan the local store applies
(``plan_local_environment_write``), inside one transaction: the tenant setting,
a lock on the tenant's mode, a read of that mode's rows, then one upsert per
planned row. Each written row is published as a snapshot delta.

The tenant must arrive as the install's tenant uuid in its canonical form. A
slug or any other form is refused, so the runtime dead-letters it: the dashboard
reads by the uuid, so a row stored under anything else is never served. The
producers resolve the tenant before they publish (the native report reads the
install's identity, which holds the uuid).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.handler_local_environment_judgement import (
    load_local_alert_rules,
)
from omnimarket.models.model_local_environment_status import (
    ModelLocalEnvironmentStatus,
)
from omnimarket.nodes.node_projection_local_environment.handlers.handler_projection_local_environment import (
    HandlerProjectionLocalEnvironment,
)
from omnimarket.nodes.node_projection_local_environment.models.model_local_environment_plan import (
    ModelLocalEnvironmentPlanRequest,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.envelope import strip_runner_injected_keys
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta
from omnimarket.projection.tenant_isolation import TENANT_GUC

# Every column a report writes, in statement order. projection_cursor is the
# table's own sequence; updated_at is the plan's stamp.
_COLUMNS = (
    "tenant_id",
    "mode",
    "service",
    "observed_state",
    "reason",
    "versions",
    "last_seen",
    "last_change",
    "verdict",
    "verdict_reason",
    "alert_code",
    "alert_severity",
    "alert_since",
    "store_kind",
    "store_location",
    "access_mode",
    "token_minted_at",
    "rotate_hint",
    "report_id",
    "updated_at",
)
_TIME_COLUMNS = frozenset(
    {"last_seen", "last_change", "alert_since", "token_minted_at", "updated_at"}
)
_KEY = ("tenant_id", "mode", "service")

_SELECT_MODE_ROWS = f"""
    SELECT {", ".join(_COLUMNS)}, projection_cursor
    FROM public.local_environment
    WHERE tenant_id = $1 AND mode = $2
    FOR UPDATE
"""

_UPSERT_ROW = (
    f"INSERT INTO public.local_environment ({', '.join(_COLUMNS)}) VALUES ("
    + ", ".join(
        f"${i}::jsonb" if name == "versions" else f"${i}"
        for i, name in enumerate(_COLUMNS, start=1)
    )
    + ") ON CONFLICT (tenant_id, mode, service) DO UPDATE SET "
    + ", ".join(f"{name} = EXCLUDED.{name}" for name in _COLUMNS if name not in _KEY)
    # The BIGSERIAL default fires on INSERT only; an updated row takes the next
    # cursor too, or a reader paging by cursor never sees the change.
    + ", projection_cursor = nextval('public.local_environment_projection_cursor_seq')"
    + f" RETURNING {', '.join(_COLUMNS)}, projection_cursor"
)


def require_tenant_uuid(tenant_id: str) -> str:
    """The tenant as the install's uuid in canonical form, or a refusal."""
    try:
        canonical = str(uuid.UUID(tenant_id))
    except ValueError:
        canonical = ""
    if canonical != tenant_id:
        raise ValueError(
            f"local environment report refused: tenant {tenant_id!r} is not a "
            "canonical tenant uuid; the producer resolves the tenant before it publishes"
        )
    return tenant_id


def _parameter(name: str, value: object) -> object:
    if value is None:
        return None
    if name == "versions":
        return json.dumps(value, sort_keys=True)
    if name in _TIME_COLUMNS and isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def _wire_row(row: dict[str, Any]) -> dict[str, Any]:
    wired: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, date | datetime):
            wired[key] = value.isoformat()
        elif isinstance(value, Decimal | uuid.UUID):
            wired[key] = str(value)
        elif key == "versions" and isinstance(value, str):
            wired[key] = json.loads(value)
        else:
            wired[key] = value
    return wired


class LocalEnvironmentProjectionWriter(BaseProjectionRunner):
    """Store each report's planned rows under (tenant, mode, service) and publish them."""

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
        self._plan = HandlerProjectionLocalEnvironment()
        self._rules = load_local_alert_rules()

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
            rows = await self._project_report(data, meta)
        finally:
            await self._stop_producer()
            await self.db.close()
        return {"rows_upserted": len(rows), "environment_rows": rows}

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_report(data, meta)
        return True

    async def _project_report(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> list[dict[str, Any]]:
        payload = strip_runner_injected_keys(data)
        payload.pop("_db", None)
        # ValidationError and the tenant refusal must reach the runtime's
        # POISON/DLQ classifier.
        report = ModelLocalEnvironmentStatus.model_validate(payload)
        tenant = require_tenant_uuid(report.tenant_id)
        written: list[dict[str, Any]] = []
        replayed: list[dict[str, Any]] = []
        async with self.db.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config($1, $2, true)", TENANT_GUC, tenant)
            # One writer per tenant's mode at a time: the plan judges every row
            # of the mode, so two reports must not both plan from the same read.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"local_environment:{tenant}:{report.mode.value}",
            )
            stored = await conn.fetch(_SELECT_MODE_ROWS, tenant, report.mode.value)
            stored_rows = [dict(r) for r in stored]
            planned = list(
                self._plan.handle(
                    ModelLocalEnvironmentPlanRequest(
                        report=report,
                        stored_rows=tuple(stored_rows),
                        rules=self._rules,
                    )
                ).rows
            )
            if not planned and any(
                str(row["service"]) == report.service
                and str(row["report_id"]) == str(report.report_id)
                for row in stored_rows
            ):
                # A publish can fail after commit. On replay, publish stored
                # facts again without writing. Include the mode's siblings:
                # their verdict deltas may also have failed in that commit.
                replayed = [_wire_row(row) for row in stored_rows]
            for row in planned:
                returned = await conn.fetch(
                    _UPSERT_ROW, *(_parameter(name, row.get(name)) for name in _COLUMNS)
                )
                written.append(_wire_row(dict(returned[0])))
        if self._snapshot_exposure is not None:
            for row in written or replayed:
                await self.publish_snapshot_delta(
                    self._snapshot_exposure,
                    op="upsert",
                    row=row,
                    source_event_id=(
                        f"{tenant}:{report.mode.value}:{row['service']}:{report.report_id}"
                    ),
                    source_topic=meta.topic,
                    source_partition=meta.partition,
                    source_offset=meta.offset,
                    tenant_id=tenant,
                )
        return written


__all__ = ["LocalEnvironmentProjectionWriter", "require_tenant_uuid"]
