"""Pydantic models for the contract-driven projection API.

These models represent the discovered configuration for each exposed projection
topic. All fields are read from contract.yaml — no convention-based defaults
for topics, columns, or ordering.
"""

from __future__ import annotations

import json
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

# The wire schema_version stamped on every snapshot delta message (Seam A,
# OMN-15800). Bumping this is a breaking change to every SnapshotCache
# consumer and every compacted onex.snapshot.projection.* topic's contents.
SNAPSHOT_DELTA_SCHEMA_VERSION = "projection_snapshot.v1"


class ProjectionStatus(StrEnum):
    """Lifecycle status of a discovered projection topic."""

    OK = "ok"
    DEGRADED = "degraded"


# An ordering column plus its direction, e.g. ("updated_at", "DESC"). Parsed
# once at contract-load time from the free-text ``order_by`` string (OMN-15799
# inheritance) so a multi-column order_by can never silently lose a sort key
# and an unknown column can never reach a query/sort at request time.
OrderDirection = Literal["ASC", "DESC"]
# Explicit NULLS FIRST|LAST placement (OMN-15800 defect A). ``None`` means the
# clause did not declare a placement — the sort falls back to the pre-existing
# default (nulls sort last, independent of ASC/DESC) so contracts that predate
# this field keep their prior behavior unchanged.
NullsPlacement = Literal["FIRST", "LAST"]
OrderBySpec = tuple[tuple[str, OrderDirection, NullsPlacement | None], ...]


class ProjectionTableConfig(BaseModel):
    """Configuration for a single projection topic, read from contract.

    All query parameters come from the ``projection_api`` section of the node's
    contract.yaml. None are inferred from column names, directory names, or
    database introspection.

    ``table``/``schema_name`` remain on this model because 55 of the 57
    exposures discovered today are still SQL-served (OMN-15800 converts the
    first 2 families; the rest strangler-migrate under follow-up tickets) —
    splitting a writer-side/serving-side model pair now would either force a
    premature full-fleet conversion or duplicate every other field across two
    types for a two-family slice. ``bus_backed``/``key_columns`` are additive,
    per-exposure fields (this model is already exposure/topic-scoped, which is
    the granularity OMN-15800's design calls for) — not a config split.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    table: str
    schema_name: str = "public"
    # tuple[str, ...] for declared columns; tuple[Literal["*"]] for SELECT *
    columns: tuple[str, ...] | tuple[Literal["*"]]
    json_columns: tuple[str, ...] = ()
    order_by: str | None = None  # None means ordering is undefined
    # Parsed form of order_by (OMN-15800 Seam C). Empty tuple when order_by is
    # None; populated at discovery/contract-load time, never at request time.
    order_by_spec: OrderBySpec = ()
    freshness_column: str | None = None  # None means freshness is unknown
    # Contract-declared expected cadence between events for this projection
    # (OMN-13035 / retro B-7). None means the topic is on-demand: it emits only
    # when triggered, so silence is a normal "idle" state and must never be
    # reported as "stale". A positive value declares the inter-event interval in
    # seconds; freshness degrades to "stale" only once the projection is behind
    # twice that interval.
    expected_event_interval_seconds: int | None = None
    cursor_column: str | None = None
    last_event_id_column: str | None = None
    last_ingest_sequence_column: str | None = None
    freshness_state_column: str | None = None
    degraded_reason_column: str | None = None
    observed_at_column: str | None = None
    limit: int = 100
    source_contract: str = ""  # node name for tracing
    status: ProjectionStatus = ProjectionStatus.OK
    degraded_reason: str = ""
    # OMN-15800: when True, this exposure is served from the bus-fed
    # SnapshotCache, never from Postgres. key_columns is required (validated
    # below) whenever bus_backed is True — it is the ordered column tuple the
    # writer uses to build the Kafka message key and the cache uses to key its
    # in-memory row map.
    bus_backed: bool = False
    key_columns: tuple[str, ...] = ()
    # OMN-15797 AC2: the ROW column carrying this exposure's per-row tenant
    # identity. ``None`` (the default, and the state of every exposure that
    # predates this field) means the exposure is not tenant-scoped and is
    # served unscoped exactly as before.
    #
    # Declaring it is a binding statement with two consequences in the serving
    # path, both fail-loud: a request whose tenant context cannot be resolved
    # is REFUSED (never a bare 200 with an empty or unscoped row list), and a
    # request that does resolve one is scoped inside
    # ``SnapshotCache.get_rows`` before the limit is applied.
    #
    # This is the ROW's own stored tenant value -- the same column the RLS
    # policy compares ``app.tenant_id`` against on the writer's side -- NOT
    # ``CachedRow.tenant_id``, which is read off a Kafka header that no
    # producer sets today and therefore defaults to the house tenant for every
    # row. Scoping on the header would be theater; scoping on the row column
    # is the value the reducer actually wrote. Per-envelope tenant identity
    # (and with it the general case for exposures that carry no tenant column)
    # remains OMN-14208.
    tenant_column: str | None = None

    @model_validator(mode="after")
    def _bus_backed_requires_key_columns(self) -> ProjectionTableConfig:
        if self.bus_backed and not self.key_columns:
            raise ValueError(
                f"projection_api exposure {self.topic!r} declares bus_backed: "
                "true but no key_columns"
            )
        return self

    @model_validator(mode="after")
    def _tenant_column_must_be_servable(self) -> ProjectionTableConfig:
        """Reject a tenant_column the serving path could not honour.

        Hard-fails contract load (like ``order_by``, unlike the fields that
        merely exclude an exposure): a typo'd or unservable tenant_column is a
        scoping declaration that would either silently not apply or scope on a
        column that is never present in the row -- returning an empty page the
        caller reads as "no data". A scoping mistake must never be a quiet one.
        """
        if self.tenant_column is None:
            return self
        if not self.bus_backed:
            raise ValueError(
                f"projection_api exposure {self.topic!r} declares "
                f"tenant_column {self.tenant_column!r} but is not bus_backed; "
                "only the bus-fed serving path can scope rows"
            )
        if self.columns != ("*",) and self.tenant_column not in {
            column.strip('"') for column in self.columns
        }:
            raise ValueError(
                f"projection_api exposure {self.topic!r} declares "
                f"tenant_column {self.tenant_column!r}, which is not among its "
                f"declared columns {list(self.columns)!r}"
            )
        return self

    @property
    def tenant_scoped(self) -> bool:
        """True when this exposure must be served under a resolved tenant."""
        return self.tenant_column is not None


class ModelProjectionSnapshotDelta(BaseModel):
    """One keyed row-delta published onto a projection snapshot topic.

    OMN-15800 Seam A. Published by :meth:`BaseProjectionRunner.publish_snapshot_delta`
    after a bus_backed exposure's row is durably written to Postgres (the
    runtime's private materialization, unchanged); consumed by
    :class:`omnimarket.projection.snapshot_cache.SnapshotCache` (Seam B).

    A ``delete`` is published as a genuine Kafka tombstone: the message VALUE
    itself is ``None`` (not a JSON body with ``op="delete"``), so compaction
    can reclaim the key. This model therefore only ever describes the
    ``upsert`` wire shape; a tombstone is represented at the transport layer,
    not by constructing an instance with ``op="delete"`` and a null value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    key: tuple[str, ...]
    op: Literal["upsert", "delete"]
    row: dict[str, Any] | None
    observed_at: str
    source_event_id: str
    # Ordering authority (CodeRabbit, OMN-15800 round 3, discussion
    # r3745850632): the SOURCE Kafka message's own coordinates, never a
    # wall-clock token. Per (source_topic, source_partition), the broker
    # assigns source_offset monotonically -- independent of which replica
    # (and which replica's clock) does the processing, so neither an NTP
    # backward step nor a rebalance to a lagging-clock replica can make a
    # genuinely newer delta look stale. `observed_at` above remains
    # display-only metadata and is never consulted for staleness.
    source_topic: str
    source_partition: int
    source_offset: int
    projection_version: str = SNAPSHOT_DELTA_SCHEMA_VERSION

    @model_validator(mode="after")
    def _row_present_iff_upsert(self) -> ModelProjectionSnapshotDelta:
        if self.op == "upsert" and self.row is None:
            raise ValueError("row is required when op == 'upsert'")
        if self.op == "delete" and self.row is not None:
            raise ValueError("row must be omitted when op == 'delete'")
        return self


def snapshot_json_value(value: Any, *, decode_json_string: bool = False) -> Any:
    """Serialize one row value for a snapshot delta / HTTP response.

    Shared by the writer path (:mod:`omnimarket.projection.runner`) and the
    serving path (:mod:`omnimarket.projection.api_server`) so both produce and
    consume the identical JSON shape for the same column value.
    """
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if decode_json_string and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


__all__ = [
    "SNAPSHOT_DELTA_SCHEMA_VERSION",
    "ModelProjectionSnapshotDelta",
    "NullsPlacement",
    "OrderBySpec",
    "OrderDirection",
    "ProjectionStatus",
    "ProjectionTableConfig",
    "snapshot_json_value",
]
