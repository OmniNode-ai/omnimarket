# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One delegation_events row as a metering record, for either store (OMN-17427).

The local SQLite evidence store and the shared lanes' Postgres table carry the
same metering columns. Both readers map a row here, so they cannot disagree
about what an unrecorded token pair or cost means. Pure: no I/O. The storage
facts behind each rule are in :mod:`omnimarket.projection.sqlite_metering_reader`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    ModelMeteringRecord,
)

# Only the columns the summary needs. Naming them keeps `prompt_text` and
# `response_text` -- which dominate the file's 120 MB -- out of the read.
METERING_COLUMNS = (
    "correlation_id",
    "created_at",
    "delegated_to",
    "model_name",
    "task_type",
    "tokens_input",
    "tokens_output",
    "cost_usd",
    "cost_savings_usd",
)


def normalise_occurred_at(raw: object) -> datetime | None:
    """Turn a ``created_at`` cell into an aware UTC instant, or ``None``.

    Accepts both storage classes the column actually holds: an ISO-8601 string
    written by the projection handler, and a unix epoch written by the DDL
    default. A naive string is read as UTC, which is what the writer emits.
    Anything unparseable returns ``None`` so the row is dropped from the window
    rather than silently dated to now. A ``datetime`` (Postgres ``TIMESTAMPTZ``
    through asyncpg, OMN-17427) is taken as it is, naive read as UTC.
    """
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, int | float):
        return datetime.fromtimestamp(float(raw), tz=UTC)
    if isinstance(raw, str) and raw:
        text = raw.strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            # A numeric epoch stored with TEXT affinity, e.g. '1781552380.36'.
            try:
                return datetime.fromtimestamp(float(text), tz=UTC)
            except ValueError:
                return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _to_decimal(raw: object) -> Decimal | None:
    """A nullable money cell as an exact Decimal, or ``None`` when unrecorded."""
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def metering_record_from_row(row: Mapping[str, Any]) -> ModelMeteringRecord | None:
    """One delegation_events row, from either store, as a metering record.

    The local SQLite store and the shared lanes' Postgres table carry the same
    METERING_COLUMNS, so both readers map rows here and cannot disagree about
    what an unrecorded token pair or cost means (OMN-17427). None when the row
    has no readable time.
    """
    occurred_at = normalise_occurred_at(row["created_at"])
    if occurred_at is None:
        return None

    tokens_in = int(row["tokens_input"] or 0)
    tokens_out = int(row["tokens_output"] or 0)
    # Both zero is the table default, not a measurement. See the reader's
    # module docstring: a completed delegation consuming no tokens in either
    # direction does not happen; an unpopulated pair of columns does.
    unrecorded_tokens = tokens_in == 0 and tokens_out == 0

    return ModelMeteringRecord(
        correlation_id=str(row["correlation_id"]),
        occurred_at=occurred_at,
        model=str(row["delegated_to"] or row["model_name"] or ""),
        task_type=str(row["task_type"] or ""),
        tokens_in=None if unrecorded_tokens else tokens_in,
        tokens_out=None if unrecorded_tokens else tokens_out,
        spend_usd=_to_decimal(row["cost_usd"]),
        recorded_savings_usd=_to_decimal(row["cost_savings_usd"]),
    )


__all__ = [
    "METERING_COLUMNS",
    "metering_record_from_row",
    "normalise_occurred_at",
]
