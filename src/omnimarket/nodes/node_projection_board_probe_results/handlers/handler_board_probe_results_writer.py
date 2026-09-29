# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect-class writer for board probe results (OMN-19937)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import yaml

from omnimarket.nodes.node_projection_board_probe_results.contract_topics import (
    SUBSCRIBE_TOPICS,
    TOPIC_BOARD_PROBE_RESULT,
)
from omnimarket.nodes.node_projection_board_probe_results.handlers.board_probe_results_fold import (
    HandlerProjectionBoardProbeResults,
)
from omnimarket.nodes.node_projection_board_probe_results.models import (
    ModelBoardProbeResultEvent,
    ModelBoardProbeResultPayload,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    deterministic_correlation_id,
)

logger = logging.getLogger(__name__)

TABLE = "omninode_internal.board_probe_results"

_UPSERT = f"""
    INSERT INTO {TABLE} (
        check_id, subject_kind, subject, repo, sha, surface_instance,
        execution_id, outcome, reasons, evidence_items, finished_at,
        source_offset, projected_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11, $12, $13)
    ON CONFLICT (
        check_id, subject_kind, repo, sha, surface_instance, execution_id
    ) DO UPDATE SET
        subject = EXCLUDED.subject,
        outcome = EXCLUDED.outcome,
        reasons = EXCLUDED.reasons,
        evidence_items = EXCLUDED.evidence_items,
        finished_at = EXCLUDED.finished_at,
        source_offset = EXCLUDED.source_offset,
        projected_at = EXCLUDED.projected_at
    WHERE {TABLE}.finished_at < EXCLUDED.finished_at
       OR (
            {TABLE}.finished_at = EXCLUDED.finished_at
            AND {TABLE}.source_offset < EXCLUDED.source_offset
       )
    RETURNING projection_cursor, check_id, subject_kind, subject, repo, sha,
              surface_instance, execution_id, outcome, reasons, evidence_items,
              finished_at, source_offset, projected_at
"""

SELECT_LATEST_PER_SUBJECT = f"""
    SELECT projection_cursor, check_id, subject_kind, subject, repo, sha,
           surface_instance, execution_id, outcome, reasons, evidence_items,
           finished_at, source_offset, projected_at
    FROM {TABLE}
    WHERE check_id = $1
      AND subject_kind = $2
      AND subject = $3
      AND repo = $4
      AND sha = $5
      AND surface_instance = $6
    ORDER BY finished_at DESC, source_offset DESC, execution_id DESC
    LIMIT 1
"""

_T = TypeVar("_T")


class BoardProbeResultsProjectionWriter(BaseProjectionRunner):
    """Validate, fold, upsert and publish accepted board probe rows."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        exposures = load_projection_exposures_from_contract(
            self._contract,
            str(self._contract.get("name", "projection_board_probe_results")),
            path,
        )
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (exposure for exposure in exposures if exposure.bus_backed), None
        )
        self._fold = HandlerProjectionBoardProbeResults()
        self._dispatch_lock = threading.Lock()

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def subscribe_topics(self) -> list[str]:
        declared = list(self._contract.get("event_bus", {}).get("subscribe_topics", []))
        if tuple(declared) != SUBSCRIBE_TOPICS:
            raise ValueError(
                "contract subscribe_topics disagree with contract_topics: "
                f"{declared!r} != {SUBSCRIBE_TOPICS!r}"
            )
        return declared

    @property
    def poison_dlq_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("dlq_topics", []))

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        publish = await self.get_publish_fn()
        if publish is None:
            raise RuntimeError(f"no publisher available for board probe DLQ {topic}")
        await publish(topic, value)

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal shim: one injected message, one guarded write."""
        data = dict(input_data)
        topics = self.subscribe_topics
        topic = str(data.pop("_topic", topics[0] if topics else ""))
        partition = int(data.pop("_partition", 0))
        offset = int(data.pop("_offset", 0))
        fallback_id = str(data.pop("_fallback_id", "")) or (
            deterministic_correlation_id(topic, partition, offset)
        )
        for key in tuple(data):
            if key.startswith("_"):
                data.pop(key)
        meta = MessageMeta(
            partition=partition,
            offset=offset,
            fallback_id=fallback_id,
            topic=topic,
        )
        with self._dispatch_lock:
            written = self._run(self._project_one_message(topic, data, meta))
        return {
            "rows_upserted": 0 if written is None else 1,
            "board_probe_result_rows": [] if written is None else [written],
        }

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any] | None:
        try:
            await self.db.connect()
            return await self._project_result(topic, data, meta)
        finally:
            await self._stop_producer()
            await self.db.close()

    @staticmethod
    def _run(coro: Coroutine[Any, Any, _T]) -> _T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_result(topic, data, meta)
        return True

    async def _project_result(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any] | None:
        if topic != TOPIC_BOARD_PROBE_RESULT:
            raise ValueError(f"unsubscribed topic {topic!r}")
        payload = ModelBoardProbeResultPayload.model_validate(data)
        event = ModelBoardProbeResultEvent.model_validate(
            {**payload.model_dump(mode="json"), "source_offset": meta.offset}
        )
        row = self._fold.fold(None, event)
        written = await self.db.execute(
            _UPSERT,
            row.check_id,
            row.subject_kind,
            row.subject,
            row.repo,
            row.sha,
            row.surface_instance,
            row.execution_id,
            row.outcome.value,
            json.dumps(list(row.reasons)),
            json.dumps(list(row.evidence_items)),
            row.finished_at,
            row.source_offset,
            datetime.now(UTC),
        )
        if not written:
            return None
        accepted = dict(written[0])
        await self._publish_snapshot(accepted, meta)
        return _jsonable(accepted)

    async def _publish_snapshot(self, row: dict[str, Any], meta: MessageMeta) -> None:
        if self._snapshot_exposure is None:
            return
        await self.publish_snapshot_delta(
            self._snapshot_exposure,
            op="upsert",
            row=_jsonable(row),
            source_event_id=meta.fallback_id,
            source_topic=meta.topic,
            source_partition=meta.partition,
            source_offset=meta.offset,
        )

    async def latest_per_subject(
        self,
        *,
        check_id: str,
        subject_kind: str,
        subject: str,
        repo: str,
        sha: str,
        surface_instance: str,
    ) -> dict[str, Any] | None:
        """Read the latest rerun for one check/subject/revision/surface."""
        rows = await self.db.execute(
            SELECT_LATEST_PER_SUBJECT,
            check_id,
            subject_kind,
            subject,
            repo,
            sha,
            surface_instance,
        )
        return None if not rows else _jsonable(dict(rows[0]))


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (
            value.isoformat()
            if isinstance(value, datetime)
            else json.loads(value)
            if key in {"reasons", "evidence_items"} and isinstance(value, str)
            else value
        )
        for key, value in row.items()
    }


__all__ = [
    "SELECT_LATEST_PER_SUBJECT",
    "TABLE",
    "_UPSERT",
    "BoardProbeResultsProjectionWriter",
]
