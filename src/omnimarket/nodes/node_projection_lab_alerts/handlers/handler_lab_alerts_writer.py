# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Postgres writer for lab alert transitions: one row per (source, subject).

It applies the pure fold (``plan_lab_alert_fold``) inside one transaction: a lock on the alert's key, a read
of the stored row, then one upsert when the fold plans one. A replayed or older transition plans nothing, so
the same transitions in any order, any number of times, leave the same rows. Each written row is published
as a snapshot delta on the contract's bus-backed exposure, before the key lock ends, so deltas for one key
reach the bus in lock order and a failed publish rolls the write back for the redelivery to plan again.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from omnimarket.handlers.delivery_cache import load_contract
from omnimarket.handlers.handler_projection_runtime_writer import (
    RuntimeProjectionWriter,
)
from omnimarket.models.model_lab_alert_transitioned import ModelLabAlertTransitioned
from omnimarket.nodes.node_projection_lab_alerts.handlers.handler_projection_lab_alerts import (
    HandlerProjectionLabAlerts,
)
from omnimarket.nodes.node_projection_lab_alerts.models.model_lab_alerts_plan import (
    ModelLabAlertsPlanRequest,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.envelope import strip_runner_injected_keys
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

TABLE = "omninode_internal.lab_alerts"

# Every column the fold plans, in statement order. projected_at is the database's write time.
_COLUMNS = (
    "alert_id",
    "source",
    "subject",
    "state",
    "severity",
    "owner",
    "detail",
    "raised_at",
    "cleared_at",
    "last_transition_id",
    "last_transition_at",
)
_TIME_COLUMNS = frozenset({"raised_at", "cleared_at", "last_transition_at"})
_KEY = "alert_id"
_RETURNED = (*_COLUMNS, "projected_at")

_SELECT_ROW = f"""
    SELECT {", ".join(_RETURNED)}
    FROM {TABLE}
    WHERE alert_id = $1
    FOR UPDATE
"""

_UPSERT_ROW = (
    f"INSERT INTO {TABLE} ({', '.join(_COLUMNS)}) VALUES ("
    + ", ".join(f"${i}" for i in range(1, len(_COLUMNS) + 1))
    + f") ON CONFLICT ({_KEY}) DO UPDATE SET "
    + ", ".join(f"{name} = EXCLUDED.{name}" for name in _COLUMNS if name != _KEY)
    + ", projected_at = NOW()"
    + f" RETURNING {', '.join(_RETURNED)}"
)


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
        else:
            wired[key] = value
    return wired


class HandlerLabAlertsProjectionRunner(BaseProjectionRunner):
    """Fold each transition into its (source, subject) row and publish the row.

    The runtime dispatches ``LabAlertsProjectionWriter`` below, which applies this runner once per delivery.
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
        self._plan = HandlerProjectionLabAlerts()

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
        # A ValidationError (a blank owner, a time without a zone, a transition id that does not match the
        # transition's own fields) must reach the runtime's POISON/DLQ classifier.
        transition = ModelLabAlertTransitioned.model_validate(payload)
        written: dict[str, Any] | None = None
        replayed: dict[str, Any] | None = None
        async with (
            self.db_for("lab_alerts", operation="write").pool.acquire() as conn,
            conn.transaction(),
        ):
            # One writer per alert key at a time: two first transitions for one key must not both plan from
            # an empty read.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"lab_alerts:{transition.alert_id}",
            )
            stored = await conn.fetch(_SELECT_ROW, transition.alert_id)
            stored_row = dict(stored[0]) if stored else None
            planned = self._plan.handle(
                ModelLabAlertsPlanRequest(transition=transition, stored_row=stored_row)
            ).row
            if planned is None:
                if stored_row is not None and str(
                    stored_row["last_transition_id"]
                ) == str(transition.transition_id):
                    # A publish can fail after commit. On replay of the same transition, publish the stored
                    # row again without writing.
                    replayed = _wire_row(stored_row)
            else:
                returned = await conn.fetch(
                    _UPSERT_ROW,
                    *(_parameter(name, planned.get(name)) for name in _COLUMNS),
                )
                written = _wire_row(dict(returned[0]))
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
                )
        return written


class LabAlertsProjectionWriter(RuntimeProjectionWriter):
    """Runtime dispatch entrypoint: apply the runner once for each delivery."""

    runner_type = HandlerLabAlertsProjectionRunner


__all__ = [
    "TABLE",
    "HandlerLabAlertsProjectionRunner",
    "LabAlertsProjectionWriter",
]
