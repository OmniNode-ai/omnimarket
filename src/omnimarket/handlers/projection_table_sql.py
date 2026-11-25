# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure SQL text and row-shaping helpers of the projection table reader.

Moved out of ``omnimarket.projection.table_reader`` unchanged so that module only
shrinks (OMN-20304); they take the exposure config by duck type.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from omnimarket.projection.models import ProjectionTableConfig


def quote_identifier(name: str) -> str:
    """Double-quote one SQL identifier taken from a contract."""
    bare = name.strip().strip('"')
    return '"' + bare.replace('"', '""') + '"'


def select_list(cfg: ProjectionTableConfig) -> str:
    if cfg.columns == ("*",):
        return "*"
    return ", ".join(quote_identifier(column) for column in cfg.columns)


def order_clause(spec: Iterable[tuple[str, str, str | None]]) -> str:
    """Render an order spec, keeping the cache's NULLS placement.

    The cache put nulls LAST whenever the contract omitted a NULLS clause,
    whatever the direction. Postgres defaults DESC to NULLS FIRST, so the
    clause is always stated.
    """
    terms = [
        f"{quote_identifier(column)} {direction} NULLS {nulls or 'LAST'}"
        for column, direction, nulls in spec
    ]
    return ", ".join(terms)


def json_value(value: Any, *, decode_json: bool = False) -> Any:
    """Serialise one column value the way the snapshot writers did."""
    if isinstance(value, datetime) or hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, UUID | Decimal):
        return str(value)
    if decode_json and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def serialise_row(cfg: ProjectionTableConfig, record: Any) -> dict[str, Any]:
    return {
        key: json_value(value, decode_json=key in cfg.json_columns)
        for key, value in dict(record).items()
    }


def _latest_per_key_sql(
    cfg: ProjectionTableConfig,
    relation: str,
    *,
    key_columns: tuple[str, ...],
    tenant_where: str,
    order_column: str,
) -> str:
    """The newest row of each key, reached by a skip scan over the key index.

    A mutable-grain exposure serves one row per key -- the latest -- and the
    snapshot cache it replaced held exactly that. A table can hold every
    window a key ever had, so a window cut by recency over the table serves a
    handful of busy keys many times and never reaches the quiet ones
    (OMN-20327: consumer-flow's 2000 newest rows named 597 of its 976
    consumer-group/topic pairs). The distinct keys are found one index probe
    each, and the newest row of each is one more, so the cost follows the key
    count, not the table's row count.

    ``key_columns`` are the declared key columns the relation carries (see
    :func:`physical_key_columns`).
    """
    keys = ", ".join(quote_identifier(column) for column in key_columns)
    tenant_and = f" AND {tenant_where}" if tenant_where else ""
    tenant_only = f" WHERE {tenant_where}" if tenant_where else ""
    after_key = (
        "("
        + ", ".join(f"t.{quote_identifier(column)}" for column in key_columns)
        + ") > ("
        + ", ".join(f"k.{quote_identifier(column)}" for column in key_columns)
        + ")"
    )
    same_key = " AND ".join(
        f"t.{quote_identifier(column)} = k.{quote_identifier(column)}"
        for column in key_columns
    )
    return (
        "WITH RECURSIVE k AS ("
        f"(SELECT {keys} FROM {relation}{tenant_only} ORDER BY {keys} LIMIT 1) "
        f"UNION ALL SELECT {', '.join(f'n.{quote_identifier(c)}' for c in key_columns)} "
        f"FROM k, LATERAL (SELECT {keys} FROM {relation} AS t "
        f"WHERE {after_key}{tenant_and} ORDER BY {keys} LIMIT 1) AS n) "
        f"SELECT l.* FROM k CROSS JOIN LATERAL (SELECT {select_list(cfg)} "
        f"FROM {relation} AS t WHERE {same_key}{tenant_and} "
        f"ORDER BY t.{quote_identifier(order_column)} DESC LIMIT 1) AS l"
    )


def _dated_first_sql(
    columns: str,
    relation: str,
    *,
    scope: list[str],
    recency: str,
    retain: int,
) -> str:
    """The newest ``retain`` rows of ``relation`` by ``recency``, NULLs last.

    The dated rows are one bounded read through the recency column's index
    (``IS NOT NULL`` is an index condition); the undated rows only fill what
    the dated rows leave, and their read is never run when that is nothing.
    ``scope`` is the window's WHERE list, applied to both reads with the same
    bound parameters.
    """
    column = quote_identifier(recency)
    where = "".join(f"{clause} AND " for clause in scope)
    return (
        f"WITH newest_dated AS (SELECT {columns} FROM {relation} "
        f"WHERE {where}{column} IS NOT NULL ORDER BY {column} DESC LIMIT {retain}) "
        "SELECT * FROM newest_dated UNION ALL "
        f"(SELECT {columns} FROM {relation} WHERE {where}{column} IS NULL "
        f"LIMIT {retain} - (SELECT count(*) FROM newest_dated))"
    )


def physical_key_columns(
    cfg: ProjectionTableConfig, relation_columns: frozenset[str] | None
) -> tuple[str, ...]:
    """The declared key columns the relation carries, in declared order.

    A key column can be part of the bus compaction key without being a column
    of the relation: the delegation and savings aggregates key on
    ``snapshot_grain``, a constant the republish query mints and the view
    deliberately lacks (OMN-19971). Such a column can never select or order a
    row, so the latest-row-per-key read keys on the rest. ``None`` means the
    relation's columns are unknown, and every declared key is kept.
    """
    if relation_columns is None:
        return cfg.key_columns
    return tuple(
        column for column in cfg.key_columns if column.strip('"') in relation_columns
    )


def key_is_unique_per_row(
    cfg: ProjectionTableConfig,
    key_columns: tuple[str, ...],
    unique_keys: tuple[frozenset[str], ...],
    not_null_columns: frozenset[str] | None = None,
) -> bool:
    """Whether every row of the relation is the only row of its key.

    Then the latest row of each key is every row, and the recursive per-key
    scan only walks the whole table to find that out: ``work.events``,
    ``session.replay`` and ``live-events`` key on an event or snapshot id, so
    the key count is the row count (OMN-19971). True when the contract
    declares the key grain immutable (one source event owns the key for its
    life), or when the relation has a unique index whose columns are all
    among ``key_columns`` -- an index on part of the key makes the whole key
    unique too, while one that reaches past it does not.

    The index route also needs every key column ``NOT NULL`` in the relation
    (``not_null_columns``). A unique index lets any number of rows hold a
    NULL, and the per-key scan never serves a row with a NULL key column (it
    matches no key by equality), so on a nullable key a window cut straight
    from the relation would serve rows the scan never did. ``None`` means the
    nullability is unknown, and the index route is not taken.
    """
    if not key_columns:
        return False
    if cfg.key_grain == "immutable":
        return True
    physical = frozenset(column.strip('"') for column in key_columns)
    if not_null_columns is None or not physical <= not_null_columns:
        return False
    return any(index and index <= physical for index in unique_keys)


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
