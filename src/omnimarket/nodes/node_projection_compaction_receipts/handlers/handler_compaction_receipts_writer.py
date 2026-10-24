# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect-class receipts writer, following the lab-proof receipts projection."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.context_install_confirm import ModelInstallConfirmResult
from omnimarket.nodes.node_projection_compaction_receipts.handlers.handler_projection_compaction_receipts import (
    HandlerProjectionCompactionReceipts,
)
from omnimarket.nodes.node_projection_compaction_receipts.models import (
    ModelCompactionReceipt,
    ModelCompactionReceiptsProjectionRequest,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.envelope import strip_runner_injected_keys
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

TABLE = "omninode_internal.compaction_receipts"
_COLUMNS = (
    "session_id",
    "receipt_key",
    "boundary_uuid",
    "boundary_kind",
    "activation_id",
    "verdict",
    "reason",
    "installed_messages_hash",
    "observed_hash",
)
_SELECT_PRIOR = f"SELECT {', '.join(_COLUMNS)} FROM {TABLE} WHERE session_id = $1"
_UPSERT = f"""
    INSERT INTO {TABLE} ({", ".join(_COLUMNS)}, source_topic, projected_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
    ON CONFLICT (session_id, receipt_key) DO UPDATE SET
        {", ".join(f"{column} = EXCLUDED.{column}" for column in _COLUMNS[2:])},
        source_topic = EXCLUDED.source_topic,
        projection_cursor = nextval('omninode_internal.compaction_receipts_projection_cursor_seq'),
        projected_at = NOW()
    WHERE {TABLE}.verdict = 'uncertain' AND EXCLUDED.verdict <> 'uncertain'
    RETURNING {", ".join(_COLUMNS)}, source_topic, projected_at, projection_cursor
"""


class CompactionReceiptsProjectionWriter(BaseProjectionRunner):
    """Persist measured verdicts; never downgrade a decided boundary on replay."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with path.open(encoding="utf-8") as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._fold = HandlerProjectionCompactionReceipts()
        exposures = load_projection_exposures_from_contract(
            self._contract, self._contract["name"], path
        )
        if len(exposures) != 1:
            raise ValueError(
                f"{path} must declare exactly one projection exposure, "
                f"found {len(exposures)}"
            )
        self._snapshot_exposure = exposures[0]

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["subscribe_topics"])

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["dlq_topics"])

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        publish = await self.get_publish_fn()
        if publish is None:
            raise RuntimeError(
                "no publisher available to quarantine a malformed receipt"
            )
        await publish(topic, value)

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        topic = str(input_data.get("_topic", self.subscribe_topics[0]))
        meta = MessageMeta(
            topic=topic,
            partition=int(input_data.get("_partition", 0)),
            offset=int(input_data.get("_offset", 0)),
            fallback_id=str(input_data.get("_fallback_id", "")),
        )
        data = {
            key: value
            for key, value in input_data.items()
            if key not in {"_topic", "_partition", "_offset", "_fallback_id", "_db"}
        }
        # Validate the entire result and fold before connecting or writing any row.
        event = ModelInstallConfirmResult.model_validate(
            strip_runner_injected_keys(data)
        )
        self._fold.handle(ModelCompactionReceiptsProjectionRequest(event=event))
        return asyncio.run(self._project_one_message(topic, event, meta))

    async def _project_one_message(
        self, topic: str, event: ModelInstallConfirmResult, meta: MessageMeta
    ) -> dict[str, Any]:
        await self.db.connect()
        try:
            rows = await self._project_result(topic, event, meta)
        finally:
            await self._stop_producer()
            await self.db.close()
        return {"rows_upserted": len(rows), "compaction_receipts_rows": rows}

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        payload = strip_runner_injected_keys(data)
        payload.pop("_db", None)
        event = ModelInstallConfirmResult.model_validate(payload)
        self._fold.handle(ModelCompactionReceiptsProjectionRequest(event=event))
        await self._project_result(topic, event, meta)
        return True

    async def _project_result(
        self, topic: str, event: ModelInstallConfirmResult, meta: MessageMeta
    ) -> list[dict[str, Any]]:
        prior = await self.db.execute(_SELECT_PRIOR, event.session_id)
        folded = self._fold.handle(
            ModelCompactionReceiptsProjectionRequest(
                event=event,
                previous_rows=tuple(
                    ModelCompactionReceipt.model_validate(dict(record))
                    for record in prior
                ),
            )
        )
        rows: list[dict[str, Any]] = []
        for row in folded.rows:
            values = row.model_dump(mode="python")
            returned = await self.db.execute(
                _UPSERT, *(values[column] for column in _COLUMNS), topic
            )
            for record in returned:
                accepted = dict(record)
                receipt = ModelCompactionReceipt.model_validate(
                    {column: accepted[column] for column in _COLUMNS}
                )
                wire = {
                    **receipt.model_dump(mode="json"),
                    **{
                        key: value.isoformat() if isinstance(value, datetime) else value
                        for key, value in accepted.items()
                        if key not in _COLUMNS
                    },
                }
                await self.publish_snapshot_delta(
                    self._snapshot_exposure,
                    op="upsert",
                    row=wire,
                    source_event_id=f"{row.session_id}:{row.receipt_key}",
                    source_topic=topic,
                    source_partition=meta.partition,
                    source_offset=meta.offset,
                )
                rows.append(wire)
        return rows


__all__ = ["CompactionReceiptsProjectionWriter"]
