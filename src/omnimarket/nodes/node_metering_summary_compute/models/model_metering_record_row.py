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

from omnimarket.models.priced_spend import priced_spend
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
#: OMN-21264: read where the store has it. A local store written before the
#: column existed holds no run handed back to its caller.
METERING_CAUSE_COLUMN = "terminal_failure_cause"
RETURNED_TO_CALLER = "returned_to_caller"
#: OMN-24615 AC1, AC5: the run's provider usage over its calls, where the reader
#: has it. No call record (NULL) means its tokens are unknown, as usage-by-model-day
#: never sees the run; any other value but "measured" keeps the tokens in the token
#: totals and counts the run as unknown tokens, with no saving.
METERING_USAGE_COLUMN = "call_usage_source"


#: Whether a SQLite store has the call table at all.
METERING_CALLS_PRESENT = "SELECT 1 FROM sqlite_master WHERE name = 'llm_call_metrics'"
# Each run's provider usage over all its calls, the rule the savings view
# applies: measured only when every call was.
_CALL_USAGE = (
    "SELECT correlation_id, CASE WHEN SUM(CASE WHEN usage_source IN "
    "('measured', 'estimated') THEN 0 ELSE 1 END) > 0 THEN 'unknown' "
    "WHEN SUM(CASE WHEN usage_source = 'measured' THEN 1 ELSE 0 END) = COUNT(*) "
    "THEN 'measured' ELSE 'estimated' END AS usage_source "
    "FROM llm_call_metrics GROUP BY correlation_id"
)


def metering_events_select(columns: tuple[str, ...], with_call_usage: bool) -> str:
    """The SQLite read of delegation_events, with each run's call usage if asked.

    OMN-24615 AC1: with ``with_call_usage`` each row also carries
    METERING_USAGE_COLUMN, NULL for a run with no call record. A store that has
    recorded no call at all holds no usage evidence, so every run reads as
    measured there and keeps its recorded tokens (the table exists on every
    store the adapter opens).
    """
    if not with_call_usage:
        return f"SELECT {', '.join(columns)} FROM delegation_events"
    named = ", ".join(f"events.{c} AS {c}" for c in columns)
    return (
        f"SELECT {named}, CASE WHEN NOT EXISTS (SELECT 1 FROM llm_call_metrics) "
        f"THEN 'measured' ELSE call_usage.usage_source END AS {METERING_USAGE_COLUMN} "
        f"FROM delegation_events AS events LEFT JOIN ({_CALL_USAGE}) AS call_usage "
        "ON call_usage.correlation_id = events.correlation_id"
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
    usage = row.get(METERING_USAGE_COLUMN, "measured")
    unrecorded_tokens = (tokens_in == 0 and tokens_out == 0) or usage is None

    return ModelMeteringRecord(
        correlation_id=str(row["correlation_id"]),
        occurred_at=occurred_at,
        model=str(row["delegated_to"] or row["model_name"] or ""),
        task_type=str(row["task_type"] or ""),
        tokens_in=None if unrecorded_tokens else tokens_in,
        tokens_out=None if unrecorded_tokens else tokens_out,
        # OMN-24615 AC3: spend only from a named price source.
        spend_usd=priced_spend(row),
        usage_measured=usage in (None, "measured"),
        recorded_savings_usd=_to_decimal(row["cost_savings_usd"]),
        returned_to_caller=row.get(METERING_CAUSE_COLUMN) == RETURNED_TO_CALLER,
        # OMN-24615 AC1: any stored failure cause, so a refused run saves nothing.
        failure_cause=str(row.get(METERING_CAUSE_COLUMN) or "") or None,
    )


__all__ = [
    "METERING_CALLS_PRESENT",
    "METERING_CAUSE_COLUMN",
    "METERING_COLUMNS",
    "METERING_USAGE_COLUMN",
    "RETURNED_TO_CALLER",
    "metering_events_select",
    "metering_record_from_row",
    "normalise_occurred_at",
]
