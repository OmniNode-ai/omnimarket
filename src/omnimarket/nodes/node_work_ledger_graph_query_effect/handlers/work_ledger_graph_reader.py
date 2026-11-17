# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a window of work_ledger_rows for the graph, read-only, through the contract's DSN variable."""

from __future__ import annotations

import importlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.work_ledger_graph import ModelWorkLedgerGraphRow

# psycopg2 ships no type stubs; bind its sql module as Any.
pgsql: Any = importlib.import_module("psycopg2.sql")


class ModelWorkLedgerGraphSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    relation: str = Field(pattern=r"^[a-z_]+\.[a-z_]+$")
    dsn_env: str = Field(min_length=1)
    default_window_days: int = Field(ge=1, le=31)


def load_graph_source(contract_path: Path) -> ModelWorkLedgerGraphSource:
    data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return ModelWorkLedgerGraphSource.model_validate(
        data["work_ledger_graph"]["ledger_source"]
    )


class ProtocolWorkLedgerGraphReader(Protocol):
    def read_rows(
        self, since: datetime, until: datetime
    ) -> list[ModelWorkLedgerGraphRow]: ...


_ROWS_SQL = (
    "SELECT row_id, row_ts, row_type, row_lane, tickets, raw_row FROM {relation} "
    "WHERE row_ts >= %s AND row_ts < %s ORDER BY row_ts, row_id"
)


class PostgresWorkLedgerGraphReader:
    """A lazy read-only connection to the contract-declared relation."""

    def __init__(self, source: ModelWorkLedgerGraphSource) -> None:
        self._source = source

    def read_rows(
        self, since: datetime, until: datetime
    ) -> list[ModelWorkLedgerGraphRow]:
        dsn = os.environ.get(self._source.dsn_env)
        if not dsn or not dsn.strip():
            raise RuntimeError(f"contract-declared {self._source.dsn_env} is unset")
        from omnimarket.projection.postgres_read_database import connect_read_only

        conn = connect_read_only(dsn)
        try:
            schema, table = self._source.relation.split(".", 1)
            query = pgsql.SQL(_ROWS_SQL).format(
                relation=pgsql.Identifier(schema, table)
            )
            with conn.cursor() as cursor:
                cursor.execute(query, (since, until))
                fetched = cursor.fetchall()
        finally:
            conn.close()
        rows: list[ModelWorkLedgerGraphRow] = []
        for row_id, row_ts, row_type, row_lane, tickets, raw_row in fetched:
            if isinstance(tickets, str):
                tickets = json.loads(tickets)
            rows.append(
                ModelWorkLedgerGraphRow(
                    row_id=str(row_id),
                    row_ts=row_ts,
                    row_type=str(row_type),
                    row_lane=row_lane,
                    tickets=tuple(str(t) for t in (tickets or [])),
                    raw_row=str(raw_row),
                )
            )
        return rows


__all__ = [
    "ModelWorkLedgerGraphSource",
    "PostgresWorkLedgerGraphReader",
    "ProtocolWorkLedgerGraphReader",
    "load_graph_source",
]
