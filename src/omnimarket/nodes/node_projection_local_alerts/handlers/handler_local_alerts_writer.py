# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Postgres writer for local alert transitions: one row per (tenant, code, subject).

It applies the same pure fold the local store applies (``plan_local_alert_fold``)
inside one transaction: the tenant setting, a lock on the alert's key, a read of
the stored row, then one upsert when the fold plans one. A replayed transition
(same derived id) or an older one plans nothing, so the same transitions in any
order, any number of times, leave the same rows. Each written row is published
as a snapshot delta.

The tenant must arrive as a canonical tenant uuid. The environment writer that
publishes transitions has already confirmed it against the tenant registry and
stores rows under the uuid the registry records; anything else is refused, so
the runtime dead-letters it and nothing is stored under a name the dashboard
never reads.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from omnimarket.handlers.delivery_cache import load_contract
from omnimarket.handlers.handler_projection_runtime_writer import (
    RuntimeProjectionWriter,
)
from omnimarket.models.model_local_alert_transitioned import (
    ModelLocalAlertTransitioned,
)
from omnimarket.nodes.node_projection_local_alerts.handlers.handler_projection_local_alerts import (
    HandlerProjectionLocalAlerts,
)
from omnimarket.nodes.node_projection_local_alerts.models.model_local_alerts_plan import (
    ModelLocalAlertsPlanRequest,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.envelope import strip_runner_injected_keys
from omnimarket.projection.error_classification import PoisonEventError
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta
from omnimarket.projection.tenant_isolation import TENANT_GUC

# Every column the fold plans, in statement order. updated_at is the database's
# write time and projection_cursor the table's own sequence.
_COLUMNS = (
    "tenant_id",
    "code",
    "subject",
    "mode",
    "service",
    "state",
    "severity",
    "raised_at",
    "cleared_at",
    "evidence",
    "last_transition_id",
    "last_transition_at",
)
_TIME_COLUMNS = frozenset({"raised_at", "cleared_at", "last_transition_at"})
_KEY = ("tenant_id", "code", "subject")
_RETURNED = (*_COLUMNS, "updated_at", "projection_cursor")

_SELECT_ROW = f"""
    SELECT {", ".join(_RETURNED)}
    FROM public.local_alerts
    WHERE tenant_id = $1 AND code = $2 AND subject = $3
    FOR UPDATE
"""

_UPSERT_ROW = (
    f"INSERT INTO public.local_alerts ({', '.join(_COLUMNS)}) VALUES ("
    + ", ".join(
        f"${i}::jsonb" if name == "evidence" else f"${i}"
        for i, name in enumerate(_COLUMNS, start=1)
    )
    + ") ON CONFLICT (tenant_id, code, subject) DO UPDATE SET "
    + ", ".join(f"{name} = EXCLUDED.{name}" for name in _COLUMNS if name not in _KEY)
    + ", updated_at = NOW()"
    # The BIGSERIAL default fires on INSERT only; an updated row takes the next
    # cursor too, or a reader paging by cursor never sees the change.
    + ", projection_cursor = nextval('public.local_alerts_projection_cursor_seq')"
    + f" RETURNING {', '.join(_RETURNED)}"
)


class LocalAlertTenantRefusedError(PoisonEventError, ValueError):
    """A transition whose tenant can never name a stored one: dead-lettered, not retried."""


def require_canonical_tenant(tenant_id: str) -> str:
    """The tenant as a canonical uuid, or a refusal the runtime dead-letters."""
    try:
        canonical = str(uuid.UUID(tenant_id))
    except ValueError:
        canonical = ""
    if canonical != tenant_id:
        raise LocalAlertTenantRefusedError(
            f"local alert transition refused: tenant {tenant_id!r} is not a "
            "canonical tenant uuid"
        )
    return tenant_id


def _parameter(name: str, value: object) -> object:
    if value is None:
        return None
    if name in _TIME_COLUMNS and isinstance(value, str):
        return datetime.fromisoformat(value)
    if name == "last_transition_id":
        return uuid.UUID(str(value))
    return value


def _wire_row(row: dict[str, Any]) -> dict[str, Any]:
    wired: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, date | datetime):
            wired[key] = value.isoformat()
        elif isinstance(value, uuid.UUID):
            wired[key] = str(value)
        elif key == "evidence" and isinstance(value, str):
            wired[key] = json.loads(value)
        else:
            wired[key] = value
    return wired


class HandlerLocalAlertsProjectionRunner(BaseProjectionRunner):
    """Fold each transition into its (tenant, code, subject) row and publish the row.

    The runtime dispatches ``LocalAlertsProjectionWriter`` below, which applies
    this runner once per delivery.
    """

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        self._contract: dict[str, Any] = load_contract(path)
        exposures = load_projection_exposures_from_contract(
            self._contract, str(self._contract["name"]), path
        )
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (exposure for exposure in exposures if exposure.bus_backed), None
        )
        self._plan = HandlerProjectionLocalAlerts()

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

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_transition(data, meta)
        return True

    async def _project_transition(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any] | None:
        payload = strip_runner_injected_keys(data)
        payload.pop("_db", None)
        # ValidationError (including a transition id that does not match the
        # transition's own fields) and the tenant refusal must reach the
        # runtime's POISON/DLQ classifier.
        transition = ModelLocalAlertTransitioned.model_validate(payload)
        tenant = require_canonical_tenant(transition.tenant_id)
        written: dict[str, Any] | None = None
        replayed: dict[str, Any] | None = None
        async with (
            self.db_for("local_alerts", operation="write").pool.acquire() as conn,
            conn.transaction(),
        ):
            await conn.execute("SELECT set_config($1, $2, true)", TENANT_GUC, tenant)
            # One writer per alert key at a time: two first transitions for one
            # key must not both plan from an empty read.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"local_alerts:{tenant}:{transition.code}:{transition.subject}",
            )
            stored = await conn.fetch(
                _SELECT_ROW, tenant, transition.code, transition.subject
            )
            stored_row = dict(stored[0]) if stored else None
            planned = self._plan.handle(
                ModelLocalAlertsPlanRequest(
                    transition=transition, stored_row=stored_row
                )
            ).row
            if planned is None:
                if stored_row is not None and str(
                    stored_row["last_transition_id"]
                ) == str(transition.transition_id):
                    # A publish can fail after commit. On replay of the same
                    # transition, publish the stored row again without writing.
                    replayed = _wire_row(stored_row)
            else:
                returned = await conn.fetch(
                    _UPSERT_ROW,
                    *(_parameter(name, planned.get(name)) for name in _COLUMNS),
                )
                written = _wire_row(dict(returned[0]))
            # Published before the key lock ends: deltas for one key reach the
            # bus in lock order, and a failed publish rolls the write back so
            # the redelivery plans it again. After the lock, two partitions'
            # deltas for one key could be reordered, and the cache accepts a
            # stale delta from a different source partition.
            row = written or replayed
            if self._snapshot_exposure is not None and row is not None:
                await self.publish_snapshot_delta(
                    self._snapshot_exposure,
                    op="upsert",
                    row=row,
                    source_event_id=str(transition.transition_id),
                    source_topic=meta.topic,
                    source_partition=meta.partition,
                    source_offset=meta.offset,
                    tenant_id=tenant,
                )
        return written


class LocalAlertsProjectionWriter(RuntimeProjectionWriter):
    """Runtime dispatch entrypoint: apply the runner once for each delivery."""

    runner_type = HandlerLocalAlertsProjectionRunner


__all__ = [
    "HandlerLocalAlertsProjectionRunner",
    "LocalAlertsProjectionWriter",
    "require_canonical_tenant",
]
