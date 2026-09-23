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

import asyncio
import json
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

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


_T = TypeVar("_T")


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


class LabLaneHealthProjectionWriter(BaseProjectionRunner):
    """The DURABLE half: writes the three lab facts and republishes.

    Two classes rather than one, which is the shape every other projection
    on the fleet uses -- see ``FleetLivenessProjectionWriter`` beside
    ``HandlerProjectionRunnerFleet``. The split is not stylistic. The
    projection wiring path injects ``_db``, ``_topic``, ``_event_type`` and
    the envelope id into the bare event dict and expects the entry it calls
    to WRITE (omnibase_infra handler_wiring.py, the projection branch). A
    pure definition-B entry cannot: it validates and returns, and the
    projection consumes, commits its offsets and stores nothing -- which is
    exactly what this node did until this change, indistinguishable from
    healthy on consumer lag and every topic watermark.

    So the runtime-facing entry lives here and takes the injected dict, and
    the pure fold stays in ``HandlerProjectionLabLaneHealth`` where a unit
    test can still falsify a verdict without a database.
    """

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

    #: Opt in to IN-PROCESS dispatch (OMN-16874). Without this the runtime
    #: classifies any runner-shaped handler -- one owning project_event, run,
    #: topics and its own adapter -- as STANDALONE, subscribes its topics and
    #: then, in its own words, "dispatches NOTHING, so it persists no rows
    #: unless a dedicated writer process is deployed for it on this lane".
    #: There is no such process for this node, which is why the lane consumed
    #: every census to LAG 0 with zero errors and stored nothing even after
    #: the two-class split landed.
    #:
    #: Declaring it is a promise the runtime cannot check: that this class
    #: scopes its connection pool to the loop that uses it, because that is
    #: the only lifetime the runtime can honour for an adapter it did not
    #: create. ``_project_one_message`` below keeps that promise by opening
    #: and closing the pool inside the same loop the work runs on, which is
    #: what ``FleetLivenessProjectionWriter`` does and why it never took the
    #: standalone branch.
    onex_runtime_inprocess_dispatch = True

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal projection shim: one injected message, one write.

        The projection wiring path hands over the bare event plus its own
        injections. ``_topic`` is popped rather than read so it never reaches
        the fold as if it were a field of the event, and the remaining keys are
        the domain payload the parsers declare.
        """
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
            topic=topic,
        )
        lanes = self._run(self._project_one_message(topic, input_data, meta))
        # ``rows_upserted`` is the key the runtime's own write-path guard reads
        # (``handler_wiring._extract_rows_upserted``); it gates the terminal
        # event on a PROVEN write and treats any other shape as zero. The
        # first revision of this class returned ``{"applied": True}``, which
        # is neither of the two shapes that guard understands, so every
        # message -- including the census message that really did write a row
        # -- was logged as "Projection handler wrote zero rows" and its
        # terminal was suppressed. Measured on the lab at 2026-09-20T11:57:31Z.
        # ``{"projected": bool}`` would also be understood and is still wrong
        # here: ``project_event`` reports success for an event that names no
        # lab lane, which is a correct no-write, so that shape would claim a
        # row on every runtime-health tick.
        return {
            "rows_upserted": len(lanes),
            "lane_rows": [lane.value for lane in lanes],
        }

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> list[EnumLabLane]:
        """Own the pool for exactly the loop this message is projected on.

        The runtime dispatches the synchronous entry above, which resolves
        its own loop; a pool opened on any other loop fails later with
        ``Event loop is closed``. Connecting and closing here is what makes
        the in-process dispatch declaration above true rather than merely
        asserted.

        ``connect()`` is INSIDE the bracket, which is the one place this
        differs from the three sibling writers, where it sits above the
        ``try``. The adversarial gate raised that shape on this PR as a
        resource leak on the connect path, by quorum. On this adapter it is
        not one -- ``AsyncpgAdapter.connect`` assigns ``self._pool`` only on
        success and ``close()`` is null-safe -- so nothing is being repaired
        here; the bracket is simply widened so the claim cannot be true of
        any future adapter either, at no cost. The siblings carry the
        narrower form and are deliberately NOT edited under this ticket: each
        is another node, and a drive-by edit to three nodes' write paths is
        not what this PR is reviewed for.

        The snapshot producer is loop-bound the same way the pool is, so it is
        stopped here too, as every sibling writer does (OMN-19355). Left cached,
        the next lab-lane write called ``send_and_wait`` on a producer whose
        sender died with the previous message's loop, and that call never
        returned: the .201 dev lane published one delta per runtime lifetime
        and then hung its consumer on the next ``compose-dev`` health tick.
        """
        try:
            await self.db.connect()
            return await self._project_and_report(topic, data, meta)
        finally:
            await self._stop_producer()
            await self.db.close()

    @staticmethod
    def _run(coro: Coroutine[Any, Any, _T]) -> _T:
        """Drive one coroutine to completion from a synchronous entry.

        ``asyncio.run`` alone is what the sibling projection writers do, and it
        raises ``RuntimeError: asyncio.run() cannot be called from a running
        event loop`` the moment a caller invokes ``handle`` from inside one.
        That is latent rather than theoretical: the entry is synchronous by the
        runtime's protocol, not by any promise about the caller's context, and
        a projection that dies on the first async caller would fail exactly the
        way this node already failed once -- at the boundary, not in the fold.

        So the loop is detected rather than assumed. With none running,
        ``asyncio.run`` is used directly. With one running, the work goes to a
        dedicated thread that owns its own loop, because the calling loop
        cannot be blocked on from inside itself.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

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
        """The base runner's boolean-returning entry; see ``_project_and_report``.

        Kept because ``BaseProjectionRunner`` declares it and the standalone
        consume loop calls it. It cannot carry the row count the in-process
        dispatch path has to report, which is why that path calls the method
        below instead of re-deriving a count from a boolean.
        """
        await self._project_and_report(topic, data, meta)
        return True

    async def _project_and_report(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> list[EnumLabLane]:
        """Write the arriving fact, then republish every lane it touched.

        Returns the lanes actually written, which is the row count the
        runtime's write-path guard needs. An empty list is a correct outcome,
        not a failure: it means the event named no lab lane.

        An event naming no lab lane is handled, not failed: raising there
        would put every non-lab runtime's health tick into a retry loop. The
        database being unavailable surfaces as an exception instead.
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
        return lanes

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


class HandlerProjectionLabLaneHealth:
    """The PURE half: one fact in, the rendered rows out.

    Touches no database and no broker, so an acceptance criterion about a
    verdict can be falsified by a unit test rather than by a live lane.
    It shares ``apply_event`` with the writer, so the two can never
    disagree about a verdict or about which lanes are in scope.
    """

    def handle(self, request: ModelLabLaneHealthRequest) -> ModelLabLaneHealthResult:
        """The canonical definition-B entrypoint: one fact in, the rows out.

        Pure. It folds the fact against in-memory state and renders the rows,
        touching no database and no broker, which is why the acceptance
        criteria can be falsified by a unit test rather than by a live lane.
        The DURABLE path is ``project_event``, which writes the same fold
        through guarded SQL and republishes; both call the same
        ``apply_event``, so the two can never disagree about a verdict or about
        which lanes are in scope.

        The topic is taken from the request's own shape rather than from a
        caller-supplied field, because the runtime adapter builds this input
        with ``input_model_cls(**payload_dict)`` over the bare event and has no
        topic to pass. See ``ModelLabLaneHealthRequest`` for why that is a
        router rather than a widening.

        A fact naming no lab lane returns ``applied=True`` with no rows. That
        is the correct outcome for a runtime on a lane this projection does not
        hold, and reporting it as a failure would put every non-lab health tick
        into a retry loop.
        """
        rows: dict[EnumLabLane, ModelLabLaneHealthRow] = {}
        touched = apply_event(
            rows, topic=request.source_topic, payload=request.as_payload()
        )
        now = datetime.now(UTC)
        return ModelLabLaneHealthResult(
            applied=True,
            rows=tuple(row.to_exposure_row(now=now) for row in touched),
        )
