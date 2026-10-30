# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect-class writer for the delegation_budget_readings projection (OMN-21326 AC3).

The second of the two classes a projection is (CLAUDE.md rule 7a, OMN-18769):
this one subclasses the projection runner base, is dispatched in process, reads
the stored row for the terminal's correlation id, calls the pure fold, and
persists the fold's result under an SQL ordering guard, so a redelivery cannot
clobber a newer terminal even if two deliveries race past the fold.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from omnimarket.nodes.node_projection_delegation_budget_readings.handlers.handler_projection_delegation_budget_readings import (
    HandlerProjectionDelegationBudgetReadings,
)
from omnimarket.nodes.node_projection_delegation_budget_readings.models import (
    ModelDelegationBudgetReadingEvent,
    ModelDelegationBudgetReadingProjectionRequest,
    ModelDelegationBudgetReadingRow,
)
from omnimarket.projection.envelope import (
    envelope_event_timestamp,
    strip_runner_injected_keys,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

TABLE_DELEGATION_BUDGET_READINGS = "omninode_internal.delegation_budget_readings"

_COLUMNS = (
    "correlation_id",
    "status",
    "task_type",
    "terminal_topic",
    "requested_timeout_seconds",
    "execution_timeout_seconds",
    "execution_duration_ms",
    "queue_wait_ms",
    "caller_lane",
    "ticket_id",
    "observed_at",
)

_SELECT_PRIOR = f"""
    SELECT {", ".join(_COLUMNS)}
    FROM {TABLE_DELEGATION_BUDGET_READINGS}
    WHERE correlation_id = $1
"""

_UPSERT = f"""
    INSERT INTO {TABLE_DELEGATION_BUDGET_READINGS} (
        {", ".join(_COLUMNS)}, projected_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
    ON CONFLICT (correlation_id)
    DO UPDATE SET
        {", ".join(f"{c} = EXCLUDED.{c}" for c in _COLUMNS[1:])},
        projected_at = NOW()
    WHERE {TABLE_DELEGATION_BUDGET_READINGS}.observed_at < EXCLUDED.observed_at
    RETURNING correlation_id, status, observed_at, projection_cursor
"""

_TRANSPORT_KEYS = frozenset({"_topic", "_partition", "_offset", "_fallback_id", "_db"})


class UntimedTerminalError(ValueError):
    """The terminal carried no envelope time; the write time would date the run wrongly."""


def _bind(row: ModelDelegationBudgetReadingRow) -> list[Any]:
    """The upsert's parameters, in column order."""
    return [getattr(row, column) for column in _COLUMNS]


def _prior_row(record: dict[str, Any]) -> ModelDelegationBudgetReadingRow:
    return ModelDelegationBudgetReadingRow.model_validate(dict(record))


def _wire(record: dict[str, Any]) -> dict[str, Any]:
    wired: dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(value, datetime):
            wired[key] = value.isoformat()
        elif isinstance(value, UUID):
            wired[key] = str(value)
        else:
            wired[key] = value
    return wired


def event_from_delivery(data: dict[str, Any]) -> ModelDelegationBudgetReadingEvent:
    """Build the typed event from one delivered terminal and its envelope time.

    The kernel seam's injected keys (``_envelope_timestamp`` and the rest) are
    read for the time and then stripped; a terminal with no envelope time is
    refused rather than stamped with the clock.
    """
    observed_at = envelope_event_timestamp(data)
    if observed_at is None:
        raise UntimedTerminalError(
            "delegate-skill terminal carried no envelope timestamp; refusing to "
            "date the run by when it was projected"
        )
    payload = strip_runner_injected_keys(data)
    topic = payload.get("_topic")
    body = {k: v for k, v in payload.items() if k not in _TRANSPORT_KEYS}
    body["terminal_topic"] = topic if isinstance(topic, str) else None
    body["observed_at"] = observed_at
    return ModelDelegationBudgetReadingEvent.model_validate(body)


class DelegationBudgetReadingsProjectionWriter(BaseProjectionRunner):
    """Persist each accepted terminal; refuse an older terminal of a correlation id."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path, encoding="utf-8") as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._fold = HandlerProjectionDelegationBudgetReadings()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(self._project_one_message(dict(input_data)))

    async def _project_one_message(self, data: dict[str, Any]) -> dict[str, Any]:
        await self.db.connect()
        try:
            written = await self._project_terminal(data)
        finally:
            await self._stop_producer()
            await self.db.close()
        return {"rows_upserted": 1 if written else 0, "row": written}

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_terminal({**data, "_topic": topic})
        return True

    async def _project_terminal(self, data: dict[str, Any]) -> dict[str, Any] | None:
        event = event_from_delivery(data)
        prior = await self.db.execute(_SELECT_PRIOR, event.correlation_id)
        result = self._fold.handle(
            ModelDelegationBudgetReadingProjectionRequest(
                event=event,
                previous_row=_prior_row(dict(prior[0])) if prior else None,
            )
        )
        if not result.applied:
            return None
        returned = await self.db.execute(_UPSERT, *_bind(result.row))
        if not returned:
            return None
        return _wire(dict(returned[0]))


__all__ = [
    "DelegationBudgetReadingsProjectionWriter",
    "UntimedTerminalError",
    "event_from_delivery",
]
