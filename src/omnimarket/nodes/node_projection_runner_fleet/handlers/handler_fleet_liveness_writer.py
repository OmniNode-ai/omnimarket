# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection writer for runner-fleet liveness (OMN-18768).

Consumes ``onex.evt.omnibase-infra.runner-fleet.v1`` -- the fleet observation the runner
monitor now emits once per cycle -- writes one row per runner, deletes the
runners that have disappeared, and publishes every write and every delete as a
snapshot delta so the exposure is genuinely bus-backed rather than declared so.

The derivation is NOT duplicated here: ``HandlerProjectionRunnerFleet`` is
imported from the pure handler, so the SQL writer and the in-memory reducer
cannot drift into disagreeing about what a row means.

The ordering rule is enforced in SQL rather than read-then-write: the
``ON CONFLICT ... WHERE`` clause refuses a write whose ``observed_at`` is not
newer than what is stored. A read-compare-write would race under concurrent
consumers and let a redelivered older observation win.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_runner_fleet.handlers.handler_projection_runner_fleet import (
    HandlerProjectionRunnerFleet,
)
from omnimarket.nodes.node_projection_runner_fleet.models import (
    ModelRunnerFleetObservationWire,
    ModelRunnerFleetProjectionRequest,
    ModelRunnerFleetRow,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

TABLE_FLEET = "omninode_internal.runner_fleet_liveness"

# The stale-write guard lives in the WHERE clause of the conflict arm. An
# observation whose observed_at is not strictly newer than the stored one is
# refused by the database, so two consumers replaying out of order cannot
# resolve to "whichever committed last".
_UPSERT_RUNNER = f"""
    INSERT INTO {TABLE_FLEET} (
        runner_name, runner_id, label_class, labels, host, observing_host,
        status, current_job_id, observed_at, first_seen_at, updated_at
    )
    VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, NOW(), NOW())
    ON CONFLICT (runner_name) DO UPDATE SET
        runner_id = EXCLUDED.runner_id,
        label_class = EXCLUDED.label_class,
        labels = EXCLUDED.labels,
        host = EXCLUDED.host,
        observing_host = EXCLUDED.observing_host,
        status = EXCLUDED.status,
        current_job_id = EXCLUDED.current_job_id,
        observed_at = EXCLUDED.observed_at,
        updated_at = NOW()
    WHERE {TABLE_FLEET}.observed_at < EXCLUDED.observed_at
    RETURNING runner_name, runner_id, label_class, labels, host,
              observing_host, status, current_job_id, observed_at,
              first_seen_at, updated_at, projection_cursor
"""

# THE TOMBSTONE SCOPE IS STALENESS, NOT THE OBSERVING HOST.
#
# The first shape of this scoped both queries on `observing_host = <this
# observer>`, reasoning that one observer's runners are not another's to
# deregister. That reasoning is right and the implementation of it was wrong,
# because the table's primary key is `runner_name` ALONE -- which it must be,
# since the exposure's snapshot key is `runner_name` and a fleet panel wants
# ONE row per runner, not one per (observer, runner) pair.
#
# With a single-column key and a host-scoped tombstone scan, two observers of
# the same org registry FIGHT over each row: every upsert rewrites
# `observing_host`, so which observer "owns" a row at tombstone time is
# whichever wrote last -- nondeterministic. A genuinely deregistered runner is
# then tombstoned only if the observer that happens to own its row is the one
# that next runs, and otherwise LINGERS REPORTING ONLINE. That is precisely
# the false-green this node exists to remove, reintroduced one layer down.
#
# Staleness is the scope that works with a single-column key. A row this
# observation SUPERSEDES -- strictly older `observed_at` -- and whose runner
# this observation did not report is deregistered, whoever observed it last.
# Both properties hold:
#
#   * one observer (the live case): every row it wrote last cycle is strictly
#     older, so behaviour is byte-identical to the host-scoped version
#   * two observers of the same registry: the second to run sees the first's
#     rows as NOT older and tombstones nothing spurious, and a genuinely
#     deregistered runner is older than both and is removed by whichever runs
#     next -- deterministically, not by whoever won the last write
#
# RESIDUAL, NAMED: two observers reporting DIFFERENT SUBSETS (different
# RUNNER_FLEET_NAME_PREFIX values) would have the narrower one tombstone the
# wider one's extra rows, which the wider one then re-creates on its next
# cycle -- a flap, not a loss. Nothing runs a second observer today and the
# prefix defaults to the whole pool for every one of them. Per-observer rows
# would need a composite key and a composite snapshot key, which is a
# different exposure from the one this ticket specifies.
_SELECT_SUPERSEDED = f"""
    SELECT runner_name FROM {TABLE_FLEET} WHERE observed_at < $1
"""

_DELETE_RUNNER = f"""
    DELETE FROM {TABLE_FLEET} WHERE runner_name = $1 AND observed_at < $2
"""


def _row_to_wire(row: ModelRunnerFleetRow, extra: dict[str, Any]) -> dict[str, Any]:
    """Merge the derived row with the database-assigned columns for publish."""
    payload: dict[str, Any] = {
        "runner_name": row.runner_name,
        "runner_id": row.runner_id,
        "label_class": row.label_class,
        "labels": list(row.labels),
        "host": row.host,
        "observing_host": row.observing_host,
        "status": row.status.value,
        "current_job_id": row.current_job_id,
        "observed_at": row.observed_at,
    }
    payload.update(extra)
    return payload


class FleetLivenessProjectionWriter(BaseProjectionRunner):
    """Projects fleet observations into ``runner_fleet_liveness``.

    The name avoids the word "runner" entirely, which takes some explaining
    in a node whose whole domain noun IS "runner": the OMN-14350 type-word
    ratchet hard-fails ``Runner`` anywhere in a class name, and its allowlist
    may only shrink, so a new entry is not an option. ``Writer`` is also the
    accurate word — this is the projection writer, the role the deployed
    ``*-writer`` services already carry. A ``Handler``-prefixed name is not
    available either: the OMN-10821 wiring check requires those to be
    importable from a Python module, which would drag the whole aiokafka
    projection-runner stack into the pure handler's import path.
    """

    #: Dispatched IN-PROCESS by the runtime auto-wiring, once per consumed
    #: message, rather than driven by its own ``run()`` consume loop. Declaring
    #: it is a promise: ``handle()`` opens one event loop per message, so every
    #: loop-bound resource this class touches -- the asyncpg pool and the
    #: snapshot producer both -- is opened and closed inside that loop. A pool
    #: cached across calls belongs to a loop that no longer exists, and
    #: reaching for it raises ``RuntimeError: Event loop is closed``.
    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)

        node_name = str(self._contract.get("name", "projection_runner_fleet"))
        exposures = load_projection_exposures_from_contract(
            self._contract, node_name, _path
        )
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (exposure for exposure in exposures if exposure.bus_backed), None
        )
        self._derive = HandlerProjectionRunnerFleet()
        # Cold start is self-healing and deliberately has no backfill
        # publisher: the monitor re-observes the WHOLE fleet every 3 minutes
        # and every observation republishes every row, so the bus-backed cache
        # reaches steady state within one monitor interval of startup. That is
        # a stronger guarantee than the per-service heartbeat families get,
        # because one producer republishes the entire key space rather than
        # each key waiting on its own subject to come back.

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim: one message, one loop, one pool."""
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
            topic=topic,
        )
        return asyncio.run(self._project_one_message(topic, input_data, meta))

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        """Project one runtime-dispatched message and report what changed.

        The returned mapping is the applied event's payload, so it carries the
        fleet facts a downstream consumer needs rather than a bare ack: a
        truthy ack over an observation that wrote nothing is indistinguishable
        from one that wrote the whole fleet.
        """
        await self.db.connect()
        try:
            written, tombstoned = await self._project_observation(data, meta)
        finally:
            await self._stop_producer()
            await self.db.close()
        return {
            "rows_upserted": len(written),
            "rows_tombstoned": len(tombstoned),
            "runner_rows": written,
            "tombstoned_runner_names": tombstoned,
        }

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone-runner entrypoint: project one message, report success."""
        await self._project_observation(data, meta)
        return True

    async def _project_observation(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> tuple[list[dict[str, Any]], list[str]]:
        observation = ModelRunnerFleetObservationWire.model_validate(data)

        # The one fact the pure derivation cannot know: which runners this host
        # already has materialized. Resolved here, by the writer that owns the
        # database, and handed IN -- which is what keeps handle() pure.
        # The rows this observation supersedes. A runner among them that this
        # observation does not report has been deregistered.
        known_rows = await self.db.execute(_SELECT_SUPERSEDED, observation.observed_at)
        known_names = tuple(str(row["runner_name"]) for row in known_rows)

        result = self._derive.handle(
            ModelRunnerFleetProjectionRequest(
                observation=observation,
                known_runner_names=known_names,
            )
        )

        written: list[dict[str, Any]] = []
        for row in result.rows:
            returned = await self.db.execute(
                _UPSERT_RUNNER,
                row.runner_name,
                row.runner_id,
                row.label_class,
                json.dumps(list(row.labels)),
                row.host,
                row.observing_host,
                row.status.value,
                row.current_job_id,
                row.observed_at,
            )
            if not returned:
                # The stale-write guard refused it: a redelivered observation
                # older than what is stored. Not an error, and NOT republished
                # -- republishing would push an older row at the cache and
                # undo a newer one.
                continue
            wire = _row_to_wire(row, dict(returned[0]))
            written.append(wire)
            await self._publish_snapshot_if_available(wire, meta, data, op="upsert")

        tombstoned: list[str] = []
        for name in result.tombstoned_runner_names:
            await self.db.execute(_DELETE_RUNNER, name, observation.observed_at)
            tombstoned.append(name)
            await self._publish_snapshot_if_available(
                None, meta, data, op="delete", key={"runner_name": name}
            )

        return written, tombstoned

    async def _publish_snapshot_if_available(
        self,
        row: dict[str, Any] | None,
        meta: MessageMeta,
        data: dict[str, Any],
        *,
        op: str,
        key: dict[str, Any] | None = None,
    ) -> None:
        """Publish one keyed delta. A no-op unless a bus_backed exposure exists.

        A ``delete`` publishes a real Kafka tombstone so the cache reclaims the
        key; a deregistered runner that kept its cached row would keep
        reporting ``online`` to every panel that reads the exposure.
        """
        if self._snapshot_exposure is None:
            return
        source_event_id = str(data.get("correlation_id") or meta.fallback_id)
        await self.publish_snapshot_delta(
            self._snapshot_exposure,
            op="upsert" if op == "upsert" else "delete",
            row=row,
            source_event_id=source_event_id,
            source_topic=meta.topic,
            source_partition=meta.partition,
            source_offset=meta.offset,
            key=key,
        )


__all__ = ["FleetLivenessProjectionWriter"]
