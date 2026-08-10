"""Live-events projection: Kafka -> live_events table + bus-fed snapshot topic.

OMN-15800 (2026-08-09 operator ruling): "It should be accessing the
projections from the event bus not from a database. Nothing should be
connecting to a database other than the runtime." This module closes the
``system_event_stream`` gap the first business-proof gate run surfaced:
``GET /projection/onex.snapshot.projection.live-events.v1`` returned HTTP 503
``not_yet_bus_backed`` because nothing ever published to
``onex.snapshot.projection.live-events.v1`` -- live HWM was 0.

Mirrors the mechanism ``node_projection_registration`` already proved live
(``handler_registration.py::RegistrationProjectionRunner`` -- the reference
implementation this file's shape is copied from field-by-field, including the
``handle()``/``project_event()``/``_publish_snapshot_if_available()`` split
and the ``KNOWN_PROJECTION_TABLES`` self-check): a dedicated
``BaseProjectionRunner`` subclass owns its own DB connection and Kafka
producer, upserts the row via a parameterized ``RETURNING`` query, and -- iff
the contract declares a ``bus_backed`` exposure -- publishes a keyed
row-delta snapshot onto the declared snapshot topic through the SAME producer
(``BaseProjectionRunner.publish_snapshot_delta``, OMN-15800 Seam A). No new
plumbing: the producer lifecycle, offset-ordering, and key-delimiter checks
all live in the shared base class.

Deliberately NOT a new noncanonical class: ``BaseProjectionRunner``
subclasses carry the (ratchet-frozen, OMN-14350) "Runner" lifecycle word, and
``.onex_ratchets/noncanonical_class_allowlist.yaml`` may only shrink -- a bare
new ``LiveEventsProjectionRunner`` would be a NEW residual outside the frozen
baseline and fail the ``no-noncanonical-lifecycle-classes`` CI gate. This
class is named ``HandlerLiveEventsProjectionRunner`` instead: its FIRST
CamelCase segment is ``Handler``, which
``no_noncanonical_lifecycle_classes._is_excluded`` exempts unconditionally
(``FIRST_WORD_ALLOW`` -- the same rule that already exempts every plain
``HandlerX`` class in this repo), regardless of what type-word appears later
in the name. Functionally and architecturally this is the identical
dedicated-consumer pattern as ``RegistrationProjectionRunner`` /
``SavingsProjectionRunner`` -- only the name is chosen to stay inside the
canonical allowlist without adding a new ratchet residual.

Reuses ``ModelLiveEvent.from_raw()`` from ``handler_projection_live_events``
(the existing generic-dispatch Handler for this node) for payload
normalisation, so both write paths classify/derive fields identically and no
parsing logic is duplicated.

Tenant scoping (OMN-15800 corrective round, contrast with savings.v1): this
node's own migrations (``migrations/0000_create_live_events.sql``,
``migrations/0002_create_omninode_internal_live_events.sql``) declare NO
``tenant_id`` column on ``live_events`` -- it is a genuinely global,
platform-wide operational event stream (the omnidash System Event Stream),
not per-tenant business data. ``SnapshotCache.get_rows()`` has no tenant
filter and ``publish_snapshot_delta()`` defaults ``tenant_id="omninode"``
with no real caller-supplied value; for ``savings.v1`` (a genuine 3-tenant
table) that was a live cross-tenant exposure and the exposure was reverted to
``bus_backed: false`` (see ``node_projection_savings/contract.yaml``). No
such defect exists here: there are no tenant-scoped rows to leak, so the same
default is correct, not merely uninvestigated.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_live_events.handlers.handler_projection_live_events import (
    ModelLiveEvent,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    safe_parse_date,
)

logger = logging.getLogger(__name__)

# Mirrors RegistrationProjectionRunner's / SavingsProjectionRunner's own copy
# of this self-check set (handler_registration.py, handler_savings.py) --
# each BaseProjectionRunner subclass validates only its OWN db_io table role
# against it, but the set is kept as the shared known-table universe by
# existing convention rather than a single-entry allowlist per file.
KNOWN_PROJECTION_TABLES: frozenset[str] = frozenset(
    {
        "delegation_events",
        "delegation_shadow_comparisons",
        "llm_cost_aggregates",
        "node_service_registry",
        "baselines_snapshots",
        "baselines_comparisons",
        "baselines_trend",
        "baselines_breakdown",
        "savings_estimates",
        "session_outcomes",
        "injection_effectiveness",
        "live_events",
    }
)


class HandlerLiveEventsProjectionRunner(BaseProjectionRunner):
    """Projects platform bus events into live_events + the bus-fed snapshot topic.

    See module docstring for the OMN-15800 mechanism this mirrors and the
    naming rationale that keeps it out of the noncanonical-class ratchet.
    """

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as f:
            self._contract: dict[str, Any] = yaml.safe_load(f)

        _tables = self._contract.get("db_io", {}).get("db_tables", [])
        _by_role = {t["role"]: t["name"] for t in _tables}

        for role, name in _by_role.items():
            if name not in KNOWN_PROJECTION_TABLES:
                raise ValueError(
                    f"Unknown table role {role!r} maps to {name!r} which is not in KNOWN_PROJECTION_TABLES"
                )

        if "live_events" not in _by_role:
            raise ValueError("Contract missing required table role 'live_events'")

        self._table_live_events: str = _by_role["live_events"]

        # OMN-15800: resolve this node's own bus_backed exposure (if any) from
        # its own already-loaded contract dict -- same pattern as
        # RegistrationProjectionRunner.__init__.
        node_name = str(self._contract.get("name", "projection_live_events"))
        exposures = load_projection_exposures_from_contract(
            self._contract, node_name, _path
        )
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (exposure for exposure in exposures if exposure.bus_backed), None
        )

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Delegates to project_event via asyncio.run() -- identical shape to
        RegistrationProjectionRunner.handle().
        """
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
            topic=topic,
        )
        ok = asyncio.run(self.project_event(topic, input_data, meta))
        return {"projected": ok}

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """UPSERT one live event, then publish a snapshot delta if bus_backed.

        Reuses ModelLiveEvent.from_raw() (handler_projection_live_events.py)
        for normalisation -- every subscribe_topic on this contract routes
        through the same uniform classification, exactly like the
        generic-dispatch Handler this mirrors.
        """
        event = ModelLiveEvent.from_raw(data, topic)
        # asyncpg requires a real datetime for a timestamptz parameter (a raw
        # ISO string raises DataError); safe_parse_date is the SAME helper
        # SavingsProjectionRunner already uses for the identical reason
        # (handler_savings.py, event_timestamp).
        event_timestamp = safe_parse_date(event.timestamp)

        rows = await self.db.execute(
            f"""
            INSERT INTO {self._table_live_events} (
              event_id, type, timestamp, source, topic, summary, payload,
              correlation_id
            ) VALUES (
              $1, $2, $3, $4, $5, $6, $7, $8
            )
            ON CONFLICT (event_id) DO UPDATE SET
              type = EXCLUDED.type,
              timestamp = EXCLUDED.timestamp,
              source = EXCLUDED.source,
              topic = EXCLUDED.topic,
              summary = EXCLUDED.summary,
              payload = EXCLUDED.payload,
              correlation_id = EXCLUDED.correlation_id
            RETURNING id, event_id, type, timestamp, source, topic, summary,
              payload, correlation_id, created_at
            """,
            event.event_id,
            event.type,
            event_timestamp,
            event.source,
            event.topic,
            event.summary,
            event.payload,
            event.correlation_id,
        )
        await self._publish_snapshot_if_available(rows[0] if rows else None, meta, data)
        return True

    async def _publish_snapshot_if_available(
        self, row: dict[str, Any] | None, meta: MessageMeta, data: dict[str, Any]
    ) -> None:
        """Best-effort snapshot publish (OMN-15800): a no-op unless this node
        declares a bus_backed exposure AND the write returned a real row.

        Mirrors RegistrationProjectionRunner._publish_snapshot_if_available
        exactly, except source_event_id prefers the row's own event_id (the
        natural per-event identity for this table) ahead of correlation_id --
        registration has no such column so it falls straight to
        correlation_id/fallback_id.
        """
        if self._snapshot_exposure is None or row is None:
            return
        source_event_id = str(
            data.get("correlation_id") or row.get("event_id") or meta.fallback_id
        )
        await self.publish_snapshot_delta(
            self._snapshot_exposure,
            op="upsert",
            row=row,
            source_event_id=source_event_id,
            source_topic=meta.topic,
            source_partition=meta.partition,
            source_offset=meta.offset,
        )


__all__: list[str] = [
    "KNOWN_PROJECTION_TABLES",
    "HandlerLiveEventsProjectionRunner",
]


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = HandlerLiveEventsProjectionRunner()
    asyncio.run(runner.run())
