# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the ledger source reads its rows (OMN-21127).

Live mode reads ``raw_row`` by row id from the contract-declared projection
relation through a lazy read-only connection. Pinned mode reads a manifest
case's markdown ledger and archive-roll prefixes, line by line, and keeps the
lines ``parse_ledger_row`` types. Both return whole row text: the 500-character
cut of the projection's state table is display detail and is never read here.
"""

from __future__ import annotations

import contextlib
import importlib
import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml
from omnibase_infra.runtime.overlay.contract_env_ref import expand_contract_env_refs
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.work_ledger_fold import (
    parse_stamp,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    LedgerRowRefusalError,
    parse_ledger_row,
)

# psycopg2 ships no type stubs; bind its sql module as Any.
pgsql: Any = importlib.import_module("psycopg2.sql")


class ModelLedgerSourceRow(BaseModel):
    """One ledger row as read: its id, its whole text and where it was read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(min_length=1)
    raw_row: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    ledger_seq: int | None = Field(default=None, ge=1, strict=True)


class ModelLedgerSourceBinding(BaseModel):
    """The contract's declared live relation and the tenants it serves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relation: str = Field(pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
    dsn_env: str = Field(min_length=1)
    binding_ref: str = Field(min_length=1)
    tenant_ids: tuple[str, ...] = ()


def load_ledger_source_binding(contract_path: Path) -> ModelLedgerSourceBinding:
    """Load and validate the contract's ``context_source_ledger.ledger_source``."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    block = raw.get("context_source_ledger") if isinstance(raw, dict) else None
    source = block.get("ledger_source") if isinstance(block, dict) else None
    if not isinstance(source, dict):
        raise ValueError("context_source_ledger.ledger_source must be a mapping")
    binding = ModelLedgerSourceBinding.model_validate(source)
    # Nested pack sources read their packaged contracts directly, so they must
    # resolve the same deploy-overlay references as top-level runtime handlers.
    # No configured tenant remains deny-all, including an unset env reference.
    return binding.model_copy(
        update={
            "tenant_ids": tuple(
                tenant
                for declared in binding.tenant_ids
                if (tenant := expand_contract_env_refs(declared).strip())
            )
        }
    )


class LedgerSourceReadError(RuntimeError):
    """A failed read, its message already redacted and type-prefixed."""


class ProtocolLedgerRowReader(Protocol):
    """Every ledger row whose stamp is at or before ``as_of``."""

    def read_rows(self, *, as_of: datetime) -> tuple[ModelLedgerSourceRow, ...]: ...


@runtime_checkable
class ProtocolLedgerSequenceReader(Protocol):
    """Every row through a sequence, including all unsequenced legacy rows."""

    def read_sequence(
        self, *, ledger_id: str, through_seq: int
    ) -> tuple[ModelLedgerSourceRow, ...]: ...


def reached_sequence(
    sequences: tuple[int | None, ...], requested: int
) -> tuple[int, bool]:
    """Contiguous unique prefix; legacy NULLs prevent claiming complete history.

    Duplicate delivery is stale even for identical rows: the projection's
    sequence index deliberately permits duplicates so consumers can report them.
    Values beyond the requested boundary do not belong to this snapshot.
    """
    counts: dict[int, int] = {}
    for value in sequences:
        if value is None or value > requested:
            continue
        counts[value] = counts.get(value, 0) + 1
    watermark = 0
    while counts.get(watermark + 1) == 1:
        watermark += 1
    return watermark, watermark == requested and None not in sequences


class PostgresLedgerRowReader:
    """Live mode: ``raw_row`` by row id from the declared projection relation."""

    def __init__(self, binding: ModelLedgerSourceBinding) -> None:
        self._binding = binding
        self._conn: Any | None = None

    def _connect(self) -> Any:
        if self._conn is not None and not self._conn.closed:
            return self._conn
        dsn = os.environ.get(self._binding.dsn_env)
        if not dsn or not dsn.strip():
            raise RuntimeError(f"contract-declared {self._binding.dsn_env} is unset")
        from omnimarket.projection.postgres_read_database import connect_read_only

        self._conn = connect_read_only(dsn)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            conn.close()

    def _redacted(self, exc: Exception) -> str:
        message = str(exc)
        dsn = os.environ.get(self._binding.dsn_env)
        if dsn:
            message = message.replace(dsn, "[redacted]")
        if (
            "postgres://" in message
            or "postgresql://" in message
            or "password=" in message
        ):
            message = "database connection/query failed (connection details redacted)"
        return f"{type(exc).__name__}: {message}"

    def read_rows(self, *, as_of: datetime) -> tuple[ModelLedgerSourceRow, ...]:
        schema, table = self._binding.relation.split(".", 1)
        query = pgsql.SQL(
            "SELECT row_id, raw_row FROM {relation} WHERE row_ts <= %s"
            " ORDER BY row_ts, row_id"
        ).format(relation=pgsql.Identifier(schema, table))
        try:
            with self._connect().cursor() as cursor:
                cursor.execute(query, (as_of,))
                fetched = list(cursor.fetchall())
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.close()
            raise LedgerSourceReadError(self._redacted(exc)) from None
        return tuple(
            ModelLedgerSourceRow(
                row_id=row_id, raw_row=raw_row, source_ref=f"{table}:{row_id}"
            )
            for row_id, raw_row in fetched
        )

    def read_sequence(
        self, *, ledger_id: str, through_seq: int
    ) -> tuple[ModelLedgerSourceRow, ...]:
        """Read one ledger's sequence snapshot without a timestamp cutoff.

        NULL rows predate sequence cutover. Include them so the caller cannot
        mistake a contiguous modern tail for complete authority history. Only a
        governed backfill can remove that stale condition; this reader never
        assigns synthetic sequences or writes the projection.
        """
        schema, table = self._binding.relation.split(".", 1)
        query = pgsql.SQL(
            "SELECT row_id, raw_row, ledger_seq FROM {relation}"
            " WHERE ledger_id = %s AND (ledger_seq <= %s OR ledger_seq IS NULL)"
            " ORDER BY ledger_seq NULLS FIRST, row_id"
        ).format(relation=pgsql.Identifier(schema, table))
        try:
            with self._connect().cursor() as cursor:
                cursor.execute(query, (ledger_id, through_seq))
                fetched = list(cursor.fetchall())
            return tuple(
                ModelLedgerSourceRow(
                    row_id=row_id,
                    raw_row=raw_row,
                    source_ref=f"{table}:{row_id}",
                    ledger_seq=ledger_seq,
                )
                for row_id, raw_row, ledger_seq in fetched
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.close()
            raise LedgerSourceReadError(self._redacted(exc)) from None


class MarkdownLedgerRowReader:
    """Pinned mode: a manifest case's ledger and archive-roll prefixes."""

    def __init__(self, paths: Sequence[Path]) -> None:
        self._paths = tuple(paths)

    def read_rows(self, *, as_of: datetime) -> tuple[ModelLedgerSourceRow, ...]:
        rows: dict[str, ModelLedgerSourceRow] = {}
        for path in self._paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise LedgerSourceReadError(f"{type(exc).__name__}: {exc}") from None
            for number, line in enumerate(lines, start=1):
                try:
                    event = parse_ledger_row(line)
                except LedgerRowRefusalError:
                    continue
                if parse_stamp(event.row_timestamp) > as_of:
                    continue
                rows.setdefault(
                    event.row_id,
                    ModelLedgerSourceRow(
                        row_id=event.row_id,
                        raw_row=event.raw_row,
                        source_ref=f"{path.name}:{number}",
                    ),
                )
        return tuple(rows.values())


__all__ = [
    "LedgerSourceReadError",
    "MarkdownLedgerRowReader",
    "ModelLedgerSourceBinding",
    "ModelLedgerSourceRow",
    "PostgresLedgerRowReader",
    "ProtocolLedgerRowReader",
    "ProtocolLedgerSequenceReader",
    "load_ledger_source_binding",
    "reached_sequence",
]
