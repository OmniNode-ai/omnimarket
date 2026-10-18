# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only Postgres effect boundary with a lazy, reusable connection."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from omnimarket.nodes.node_projection_freshness_alert_effect.models import (
    FreshnessPolicyError,
    ModelFreshnessCheck,
    ModelFreshnessObservation,
    ModelReadsSource,
)

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")


def validate_identifier(value: str) -> str:
    """Reject SQL fragments before interpolation, even for constructed models."""
    if not _IDENTIFIER.fullmatch(value):
        raise FreshnessPolicyError(f"not a plain SQL identifier: {value!r}")
    return value


def resolve_reads_source_dsn(source: ModelReadsSource) -> str:
    """Use only the contract-named DSN; never guess a database identity."""
    try:
        dsn = os.environ[source.dsn_env]
    except KeyError:
        dsn = ""
    if not dsn.strip():
        raise FreshnessPolicyError(
            f"{source.dsn_env} is unset or empty for binding {source.binding_ref!r}"
        )
    return dsn


class PostgresFreshnessReader:
    """Read both aggregates using one read-only connection; reconnect on error."""

    def __init__(self, *, dsn: str, now: Callable[[], datetime]) -> None:
        if not dsn.strip():
            raise FreshnessPolicyError("freshness reads DSN is empty")
        self._dsn = dsn
        self._now = now
        self._conn: Any | None = None

    def _connect(self) -> Any:
        if self._conn is not None and getattr(self._conn, "closed", 0) == 0:
            return self._conn
        from omnimarket.projection.postgres_read_database import connect_read_only

        self._conn = connect_read_only(self._dsn)
        return self._conn

    def close(self) -> None:
        """Drop a failed connection before another check attempts a read."""
        if self._conn is not None:
            connection, self._conn = self._conn, None
            connection.close()

    def read(self, check: ModelFreshnessCheck) -> ModelFreshnessObservation:
        """Interpolate validated identifiers and bind the demand cutoff value."""
        supply = validate_identifier(check.supply.relation)
        supply_timestamp = validate_identifier(check.supply.timestamp_column)
        demand = validate_identifier(check.demand.relation)
        demand_timestamp = validate_identifier(check.demand.timestamp_column)
        distinct = validate_identifier(check.demand.distinct_column)
        cutoff = self._now() - timedelta(seconds=check.demand.window_seconds)
        try:
            connection = self._connect()
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT max({supply_timestamp}) FROM {supply}")
                supply_row = cursor.fetchone()
                cursor.execute(
                    f"SELECT count(DISTINCT {distinct}) FROM {demand} "
                    f"WHERE {demand_timestamp} > %s",
                    (cutoff,),
                )
                demand_row = cursor.fetchone()
            if supply_row is None or demand_row is None:
                raise ValueError("freshness aggregate returned no result row")
            return ModelFreshnessObservation(
                supply_newest_at=supply_row[0],
                demand_distinct_active=demand_row[0],
            )
        except Exception:
            self.close()
            raise
