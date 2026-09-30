# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect-class writer for the lab_proof_receipts projection (OMN-19566).

The second of the two classes a projection is (CLAUDE.md rule 7a, OMN-18769):
this one subclasses the projection runner base, is dispatched in process, reads
the stored row for the event's key, calls the pure fold, and persists the fold's
result under an SQL ordering guard, so a redelivery cannot clobber a newer proof
even if two deliveries race past the fold.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_lab_proof_receipts.handlers.handler_projection_lab_proof_receipts import (
    HandlerProjectionLabProofReceipts,
)
from omnimarket.nodes.node_projection_lab_proof_receipts.models import (
    ModelLabProofReceiptEvent,
    ModelLabProofReceiptProjectionRequest,
    ModelLabProofReceiptRow,
)
from omnimarket.projection.envelope import strip_runner_injected_keys
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

TABLE_LAB_PROOF_RECEIPTS = "omninode_internal.lab_proof_receipts"

_COLUMNS = (
    "repo",
    "pr_number",
    "head_sha",
    "profile_id",
    "profile_version",
    "receipt_key",
    "handler_kind",
    "result",
    "verifier_token",
    "verifier_reason",
    "mandatory_checks",
    "missing_mandatory_checks",
    "failing_checks",
    "started_at",
    "finished_at",
    "runner_identity",
    "verifier_identity",
    "host",
    "slot",
    "carried_from",
    "receipt",
)
_JSON_COLUMNS = frozenset(
    {"mandatory_checks", "missing_mandatory_checks", "failing_checks", "receipt"}
)

_SELECT_PRIOR = f"""
    SELECT {", ".join(_COLUMNS)}
    FROM {TABLE_LAB_PROOF_RECEIPTS}
    WHERE repo = $1 AND pr_number = $2 AND head_sha = $3
      AND profile_id = $4 AND profile_version = $5
"""

_UPSERT = f"""
    INSERT INTO {TABLE_LAB_PROOF_RECEIPTS} (
        {", ".join(_COLUMNS)}, projected_at
    )
    VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
        $11::jsonb, $12::jsonb, $13::jsonb, $14, $15, $16, $17, $18, $19, $20,
        $21::jsonb, NOW()
    )
    ON CONFLICT (repo, pr_number, head_sha, profile_id, profile_version)
    DO UPDATE SET
        {", ".join(f"{c} = EXCLUDED.{c}" for c in _COLUMNS[5:])},
        projected_at = NOW()
    WHERE {TABLE_LAB_PROOF_RECEIPTS}.finished_at < EXCLUDED.finished_at
    RETURNING receipt_key, result, verifier_token, finished_at, projection_cursor
"""


def _bind(row: ModelLabProofReceiptRow) -> list[Any]:
    """The upsert's parameters, JSON columns serialized for the ::jsonb casts."""
    values: list[Any] = []
    for column in _COLUMNS:
        value = getattr(row, column)
        if column in _JSON_COLUMNS:
            value = json.dumps(list(value) if isinstance(value, tuple) else value)
        elif isinstance(value, Enum):
            value = value.value
        values.append(value)
    return values


def _prior_row(record: dict[str, Any]) -> ModelLabProofReceiptRow:
    """A stored row back into the model; asyncpg hands jsonb back as text."""
    data = dict(record)
    for column in _JSON_COLUMNS:
        if isinstance(data.get(column), str):
            data[column] = json.loads(data[column])
    return ModelLabProofReceiptRow.model_validate(data)


def _wire(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in record.items()
    }


class LabProofReceiptsProjectionWriter(BaseProjectionRunner):
    """Persist each accepted pr-head receipt; refuse an older proof of a key."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path, encoding="utf-8") as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._fold = HandlerProjectionLabProofReceipts()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        # The in-process dispatch hands over the payload with the delivery's
        # topic, partition and offset beside it. This projection publishes no
        # snapshot delta, so none of them is needed past this point.
        data = {
            key: value
            for key, value in input_data.items()
            if key not in {"_topic", "_partition", "_offset", "_fallback_id"}
        }
        return asyncio.run(self._project_one_message(data))

    async def _project_one_message(self, data: dict[str, Any]) -> dict[str, Any]:
        await self.db.connect()
        try:
            written = await self._project_receipt(data)
        finally:
            await self._stop_producer()
            await self.db.close()
        return {"rows_upserted": 1 if written else 0, "row": written}

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_receipt(data)
        return True

    async def _project_receipt(self, data: dict[str, Any]) -> dict[str, Any] | None:
        # The kernel seam's injected keys (_db, _event_type, _envelope_id, ...)
        # are stripped before the extra="forbid" model; a field a producer put
        # on the wire still fails validation and dead-letters (OMN-19716).
        payload = strip_runner_injected_keys(data)
        payload.pop("_db", None)
        event = ModelLabProofReceiptEvent.model_validate(payload)
        prior = await self.db.execute(
            _SELECT_PRIOR,
            event.repo,
            event.pr_number,
            event.head_sha,
            event.profile_id,
            event.profile_version,
        )
        result = self._fold.handle(
            ModelLabProofReceiptProjectionRequest(
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


__all__ = ["LabProofReceiptsProjectionWriter"]
