# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection runner for the lab lane-health exposure (OMN-18769).

Consumes the three lab facts, writes one row per lane, and republishes each
written row as a keyed snapshot delta so the exposure is genuinely bus-backed.

**The fold logic is not duplicated here.** Every verdict and every drop
decision comes from ``lane_health_fold`` and ``enum_fact_status``, so the SQL
writer and the pure reducer cannot drift into disagreeing about what STALE
means or about which lanes are in scope.

**Ordering is enforced in SQL, not read-then-write.** Each fact kind writes its
own column group under an ``ON CONFLICT (lane) DO UPDATE ... WHERE`` guard
comparing ``observed_at``, so an out-of-order redelivery of an older census
cannot clobber a newer one, and the three concurrent consume loops never race
each other's columns. A whole-row read-modify-write would have all three
problems at once.

**What the published decay means, stated rather than left to be discovered.**
The snapshot delta carries each fact's decayed verdict as of the instant the
row was written, AND carries that fact's ``observed_at`` and pre-decay verdict.
The decayed value therefore ages after publication: a lane whose producers all
stop emitting freezes at its last-written verdict. That is not hidden — it is
why the inputs travel with it. A consumer re-derives the current verdict by
calling the same :func:`~...enum_fact_status.decay` function on the shipped
inputs, which is what makes "every consumer decays identically" true rather
than aspirational. Storing a decayed verdict as the only answer would need a
timer to keep it true, and a timer that stops produces a confident stale green.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_lab_lane_health.handlers.lane_health_fold import (
    SUBSCRIBE_TOPICS,
    TOPIC_LAB_PASS_RECEIPT,
    TOPIC_LANE_CENSUS,
    TOPIC_RUNTIME_HEALTH,
    LaneHealthFoldError,
    apply_event,
    census_facts,
    health_fact,
    receipt_fact,
)
from omnimarket.nodes.node_projection_lab_lane_health.handlers.lane_health_fold import (
    parse_timestamp as _parse_ts,
)
from omnimarket.nodes.node_projection_lab_lane_health.models.enum_lab_lane import (
    EnumLabLane,
)
from omnimarket.nodes.node_projection_lab_lane_health.models.model_lab_lane_health_request import (
    ModelLabLaneHealthRequest,
)
from omnimarket.nodes.node_projection_lab_lane_health.models.model_lab_lane_health_result import (
    ModelLabLaneHealthResult,
)
from omnimarket.nodes.node_projection_lab_lane_health.models.model_lab_lane_health_row import (
    ModelLabLaneHealthRow,
    ModelLaneCensusFact,
    ModelLaneHealthFact,
    ModelLaneReceiptFact,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

TABLE = "omninode_internal.lab_lane_health"

#: Written on every upsert regardless of which fact arrived, because a row that
#: exists must name its lane class for a reader that filters on it.
_ROW_DEFAULTS = "lane_class = EXCLUDED.lane_class, projected_at = EXCLUDED.projected_at"

_UPSERT_CENSUS = f"""
    INSERT INTO {TABLE} (
        lane, lane_class, projected_at,
        census_observed_at, census_original_status, census_drift_count,
        census_drift_items, census_host
    )
    VALUES ($1, 'lab', $2, $3, $4, $5, $6::jsonb, $7)
    ON CONFLICT (lane) DO UPDATE SET
        {_ROW_DEFAULTS},
        census_observed_at = EXCLUDED.census_observed_at,
        census_original_status = EXCLUDED.census_original_status,
        census_drift_count = EXCLUDED.census_drift_count,
        census_drift_items = EXCLUDED.census_drift_items,
        census_host = EXCLUDED.census_host
    WHERE {TABLE}.census_observed_at IS NULL
       OR {TABLE}.census_observed_at <= EXCLUDED.census_observed_at
"""

_UPSERT_HEALTH = f"""
    INSERT INTO {TABLE} (
        lane, lane_class, projected_at,
        health_observed_at, health_original_status, health_aggregate,
        health_dimensions
    )
    VALUES ($1, 'lab', $2, $3, $4, $5, $6::jsonb)
    ON CONFLICT (lane) DO UPDATE SET
        {_ROW_DEFAULTS},
        health_observed_at = EXCLUDED.health_observed_at,
        health_original_status = EXCLUDED.health_original_status,
        health_aggregate = EXCLUDED.health_aggregate,
        health_dimensions = EXCLUDED.health_dimensions
    WHERE {TABLE}.health_observed_at IS NULL
       OR {TABLE}.health_observed_at <= EXCLUDED.health_observed_at
"""

_UPSERT_RECEIPT = f"""
    INSERT INTO {TABLE} (
        lane, lane_class, projected_at,
        receipt_observed_at, receipt_original_status, receipt_sha,
        receipt_result, receipt_failing_checks
    )
    VALUES ($1, 'lab', $2, $3, $4, $5, $6, $7::jsonb)
    ON CONFLICT (lane) DO UPDATE SET
        {_ROW_DEFAULTS},
        receipt_observed_at = EXCLUDED.receipt_observed_at,
        receipt_original_status = EXCLUDED.receipt_original_status,
        receipt_sha = EXCLUDED.receipt_sha,
        receipt_result = EXCLUDED.receipt_result,
        receipt_failing_checks = EXCLUDED.receipt_failing_checks
    WHERE {TABLE}.receipt_observed_at IS NULL
       OR {TABLE}.receipt_observed_at <= EXCLUDED.receipt_observed_at
"""

_SELECT_ROW = f"""
    SELECT row_to_json(t)
    FROM (
        SELECT lane,
               census_observed_at, census_drift_count, census_drift_items,
               census_host,
               health_observed_at, health_aggregate, health_dimensions,
               receipt_observed_at, receipt_sha, receipt_result,
               receipt_failing_checks
        FROM {TABLE}
        WHERE lane = $1
    ) t
"""


def _json_list(raw: Any) -> list[dict[str, str]]:
    """Decode a JSONB column that asyncpg may hand back as text or as a list."""
    if isinstance(raw, str):
        raw = json.loads(raw or "[]")
    if not isinstance(raw, list):
        return []
    return [
        {str(k): str(v) for k, v in item.items()}
        for item in raw
        if isinstance(item, dict)
    ]


def _json_strings(raw: Any) -> tuple[str, ...]:
    """Decode a JSONB array of plain strings (the failing-check names)."""
    if isinstance(raw, str):
        raw = json.loads(raw or "[]")
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw)


def row_from_record(record: dict[str, Any]) -> ModelLabLaneHealthRow:
    """Rebuild the typed row from the stored record.

    Reading the row BACK before publishing (rather than publishing the fact
    that just arrived) is what makes the snapshot a statement about the lane
    rather than about the event: a health event must republish the census and
    receipt facts alongside it, or a consumer keyed on ``lane`` would see the
    other two dimensions blank out on every health tick.
    """
    lane = EnumLabLane(str(record["lane"]))
    census = None
    if record.get("census_observed_at") is not None:
        census = ModelLaneCensusFact(
            observed_at=_parse_ts(record["census_observed_at"], "census_observed_at"),
            drift_count=int(record.get("census_drift_count") or 0),
            drift_items=tuple(_json_list(record.get("census_drift_items"))),
            host=str(record.get("census_host") or ""),
        )
    health = None
    if record.get("health_observed_at") is not None:
        health = ModelLaneHealthFact(
            observed_at=_parse_ts(record["health_observed_at"], "health_observed_at"),
            aggregate=str(record.get("health_aggregate") or ""),
            dimensions=tuple(_json_list(record.get("health_dimensions"))),
        )
    receipt = None
    if record.get("receipt_observed_at") is not None:
        receipt = ModelLaneReceiptFact(
            observed_at=_parse_ts(record["receipt_observed_at"], "receipt_observed_at"),
            sha=str(record.get("receipt_sha") or ""),
            result=str(record.get("receipt_result") or ""),
            failing_checks=_json_strings(record.get("receipt_failing_checks")),
        )
    return ModelLabLaneHealthRow(
        lane=lane, census=census, health=health, receipt=receipt
    )


class HandlerProjectionLabLaneHealth(BaseProjectionRunner):
    """Projects the three lab facts into ``omninode_internal.lab_lane_health``."""

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        exposures = load_projection_exposures_from_contract(
            self._contract,
            str(self._contract.get("name", "projection_lab_lane_health")),
            path,
        )
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (exposure for exposure in exposures if exposure.bus_backed), None
        )

    def handle(self, request: ModelLabLaneHealthRequest) -> ModelLabLaneHealthResult:
        """The canonical definition-B entrypoint: one fact in, the rows out.

        Pure. It folds the fact against in-memory state and renders the rows,
        touching no database and no broker, which is why the acceptance
        criteria can be falsified by a unit test rather than by a live lane.
        The DURABLE path is ``project_event``, which writes the same fold
        through guarded SQL and republishes; both call the same
        ``apply_event``, so the two can never disagree about a verdict or about
        which lanes are in scope.

        A fact naming no lab lane returns ``applied=True`` with no rows. That
        is the correct outcome for a runtime on a lane this projection does not
        hold, and reporting it as a failure would put every non-lab health tick
        into a retry loop.
        """
        rows: dict[EnumLabLane, ModelLabLaneHealthRow] = {}
        touched = apply_event(rows, topic=request.topic, payload=request.payload)
        now = datetime.now(UTC)
        return ModelLabLaneHealthResult(
            applied=True,
            rows=tuple(row.to_exposure_row(now=now) for row in touched),
        )

    @property
    def topics(self) -> list[str]:
        """The base runner's subscription list.

        One alias for one list rather than two sources: the contract is the
        authority and :meth:`subscribe_topics` is the check that the code
        agrees with it.
        """
        return self.subscribe_topics

    @property
    def subscribe_topics(self) -> list[str]:
        declared = list(self._contract.get("event_bus", {}).get("subscribe_topics", []))
        # The contract is the wiring authority; the fold module's tuple is the
        # code's own view of the same set. They must agree, and a mismatch is a
        # wiring bug that would otherwise surface as a silently unconsumed
        # topic.
        if sorted(declared) != sorted(SUBSCRIBE_TOPICS):
            raise LaneHealthFoldError(
                "contract subscribe_topics disagree with lane_health_fold: "
                f"{sorted(declared)} != {sorted(SUBSCRIBE_TOPICS)}"
            )
        return declared

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Write the arriving fact, then republish every lane it touched.

        Returns ``True`` when the event was handled, including the case where
        it named no lab lane and therefore changed nothing: that is a correct
        outcome, not a projection failure, and reporting it as failure would
        put every non-lab runtime's health tick into a retry loop. ``False`` is
        reserved for the base runner's own meaning -- the database was
        unavailable -- which surfaces as an exception here instead.
        """
        now = datetime.now(UTC)
        lanes: list[EnumLabLane] = []

        if topic == TOPIC_LANE_CENSUS:
            for lane, census in census_facts(data).items():
                await self._db.execute(
                    _UPSERT_CENSUS,
                    lane.value,
                    now,
                    census.observed_at,
                    census.status.value,
                    census.drift_count,
                    json.dumps([dict(item) for item in census.drift_items]),
                    census.host,
                )
                lanes.append(lane)
        elif topic == TOPIC_RUNTIME_HEALTH:
            resolved_health = health_fact(data)
            if resolved_health is not None:
                lane, health = resolved_health
                await self._db.execute(
                    _UPSERT_HEALTH,
                    lane.value,
                    now,
                    health.observed_at,
                    health.status.value,
                    health.aggregate,
                    json.dumps([dict(d) for d in health.dimensions]),
                )
                lanes.append(lane)
        elif topic == TOPIC_LAB_PASS_RECEIPT:
            resolved_receipt = receipt_fact(data)
            if resolved_receipt is not None:
                lane, receipt = resolved_receipt
                await self._db.execute(
                    _UPSERT_RECEIPT,
                    lane.value,
                    now,
                    receipt.observed_at,
                    receipt.status.value,
                    receipt.sha,
                    receipt.result,
                    json.dumps(list(receipt.failing_checks)),
                )
                lanes.append(lane)
        else:
            raise LaneHealthFoldError(f"unsubscribed topic {topic!r}")

        for lane in lanes:
            await self._republish(lane, meta=meta, now=now)
        return True

    async def _republish(
        self, lane: EnumLabLane, *, meta: MessageMeta, now: datetime
    ) -> None:
        """Read the whole row back and publish it as one keyed snapshot delta.

        The read-back is the point. Publishing only the fact that just arrived
        would blank the other two dimensions on every consumer keyed on
        ``lane``: the snapshot is a statement about the LANE, not about the
        event.
        """
        exposure = self._snapshot_exposure
        if exposure is None:
            return
        document = await self._db.fetchval(_SELECT_ROW, lane.value)
        if document is None:
            # The guarded upsert refused the write as out-of-order and no row
            # existed. Nothing changed, so nothing is republished -- silence is
            # the correct output, not an empty row.
            return
        record = json.loads(document) if isinstance(document, str) else dict(document)
        row = row_from_record(record)
        await self.publish_snapshot_delta(
            exposure,
            op="upsert",
            row=row.to_exposure_row(now=now),
            source_event_id=meta.fallback_id,
            source_topic=meta.topic,
            source_partition=meta.partition,
            source_offset=meta.offset,
        )
