# SPDX-License-Identifier: MIT
"""Persist accepted demo-readiness rows and publish keyed snapshot deltas."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_demo_readiness.handlers.handler_projection_demo_readiness import (
    HandlerProjectionDemoReadiness,
)
from omnimarket.nodes.node_projection_demo_readiness.models import (
    ModelDemoReadinessProjectionRequest,
    ModelDemoReadinessRow,
)
from omnimarket.nodes.node_projection_demo_readiness.terminal import (
    TERMINAL_TOPICS,
    parse_demo_terminal,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.error_classification import PoisonEventError
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

TABLE_DEMO_READINESS = "omninode_internal.demo_readiness_latest"

_SELECT_PRIOR = f"""
    SELECT node_id, run_id, status, dashboard_configuration, observed_at,
           source_event_id, evidence_path, dry_run, failure_count,
           demo_blocker_count, demo_degraded_count, total_finding_count,
           projection_cursor
    FROM {TABLE_DEMO_READINESS} WHERE node_id = $1
"""

_UPSERT = f"""
    INSERT INTO {TABLE_DEMO_READINESS} (
        node_id, run_id, status, dashboard_configuration, observed_at,
        source_event_id, evidence_path, dry_run, failure_count,
        demo_blocker_count, demo_degraded_count, total_finding_count
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
    ON CONFLICT (node_id) DO UPDATE SET
        run_id = EXCLUDED.run_id,
        status = EXCLUDED.status,
        dashboard_configuration = EXCLUDED.dashboard_configuration,
        observed_at = EXCLUDED.observed_at,
        source_event_id = EXCLUDED.source_event_id,
        evidence_path = EXCLUDED.evidence_path,
        dry_run = EXCLUDED.dry_run,
        failure_count = EXCLUDED.failure_count,
        demo_blocker_count = EXCLUDED.demo_blocker_count,
        demo_degraded_count = EXCLUDED.demo_degraded_count,
        total_finding_count = EXCLUDED.total_finding_count,
        projection_cursor = nextval(
            pg_get_serial_sequence('{TABLE_DEMO_READINESS}', 'projection_cursor')
        )
    WHERE ({TABLE_DEMO_READINESS}.observed_at, {TABLE_DEMO_READINESS}.source_event_id)
        < (EXCLUDED.observed_at, EXCLUDED.source_event_id)
    RETURNING node_id, run_id, status, dashboard_configuration, observed_at,
              source_event_id, evidence_path, dry_run, failure_count,
              demo_blocker_count, demo_degraded_count, total_finding_count,
              projection_cursor
"""


def _wire_row(row: ModelDemoReadinessRow) -> dict[str, Any]:
    return row.model_dump(mode="json")


class DemoReadinessProjectionWriter(BaseProjectionRunner):
    """The sole runtime-dispatched writer; the pure fold is called in-process."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with path.open(encoding="utf-8") as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        exposures = load_projection_exposures_from_contract(
            self._contract, str(self._contract["name"]), path
        )
        self._snapshot_exposure: ProjectionTableConfig = next(
            exposure for exposure in exposures if exposure.bus_backed
        )
        self._derive = HandlerProjectionDemoReadiness()
        if frozenset(self.subscribe_topics) != TERMINAL_TOPICS:
            raise ValueError(
                "demo-readiness terminal parser and contract topics disagree"
            )

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["subscribe_topics"])

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        data = dict(input_data)
        topic = str(data.pop("_topic"))
        meta = MessageMeta(
            partition=int(data.pop("_partition")),
            offset=int(data.pop("_offset")),
            fallback_id=str(data.pop("_fallback_id", "")),
            topic=topic,
        )
        return asyncio.run(self._project_one_message(topic, data, meta))

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        await self.db.connect()
        try:
            row = await self._project_event(topic, data, meta)
        finally:
            await self._stop_producer()
            await self.db.close()
        return {"rows_upserted": int(row is not None), "demo_readiness_row": row}

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_event(topic, data, meta)
        return True

    async def _project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any] | None:
        observation = parse_demo_terminal(topic, data)
        prior_rows = await self.db.execute(_SELECT_PRIOR, observation.node_id)
        previous = (
            ModelDemoReadinessRow.model_validate(dict(prior_rows[0]))
            if prior_rows
            else None
        )
        if (
            previous is not None
            and previous.source_event_id == observation.source_event_id
            and previous.model_copy(update={"projection_cursor": None}) != observation
        ):
            raise PoisonEventError(
                "demo terminal reused an event identity with different payload"
            )
        result = self._derive.handle(
            ModelDemoReadinessProjectionRequest(
                previous_row=previous, observation=observation
            )
        )
        if not result.applied:
            # The DB write can commit before a snapshot publish fails. On
            # redelivery, the same event must retry that publish rather than
            # being discarded as an already-applied DB row.
            if (
                previous is not None
                and previous.ordering_key == observation.ordering_key
            ):
                await self._publish_row(previous, meta)
            return None
        candidate = result.row
        returned = await self.db.execute(
            _UPSERT,
            candidate.node_id,
            candidate.run_id,
            candidate.status.value,
            candidate.dashboard_configuration.value,
            candidate.observed_at,
            candidate.source_event_id,
            candidate.evidence_path,
            candidate.dry_run,
            candidate.failure_count,
            candidate.demo_blocker_count,
            candidate.demo_degraded_count,
            candidate.total_finding_count,
        )
        if not returned:
            return None
        written = ModelDemoReadinessRow.model_validate(dict(returned[0]))
        await self._publish_row(written, meta)
        return _wire_row(written)

    async def _publish_row(self, row: ModelDemoReadinessRow, meta: MessageMeta) -> None:
        wire = _wire_row(row)
        published = await self.publish_snapshot_delta(
            self._snapshot_exposure,
            op="upsert",
            row=wire,
            source_event_id=str(row.source_event_id),
            source_topic=meta.topic,
            source_partition=meta.partition,
            source_offset=meta.offset,
        )
        if not published:
            raise RuntimeError("demo-readiness snapshot delta was not published")


__all__ = ["DemoReadinessProjectionWriter"]
