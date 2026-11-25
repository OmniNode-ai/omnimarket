# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A text freshness column read as an instant in a windowed projection read (OMN-21288).

``metering_summary.as_of`` is ISO-8601 TEXT, so comparing it with a timestamptz
failed every ``window=7d`` read. The expression here casts it behind a
range-checked pattern; a value that is not an instant is NULL and is counted,
never a cast error.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


_TEXT_TYPES = ("text", "character varying", "character", "varchar", "bpchar")

# An ISO-8601 instant, range-checked so the cast below cannot raise: a text
# freshness column holds whatever its writer put there, and one malformed row
# must not fail the read of every other row (OMN-21288). Every shape the cast
# refuses is excluded here: a day the month lacks (Feb 30 and 31, day 31 of a
# 30-day month, Feb 29 of a year that is not a leap year), year 0000, and a zone
# offset past the +-15:59 the database accepts. Digits are [0-9], never \d, so a
# non-ASCII digit under a Unicode locale cannot pass.
_LEAP_YEAR = r"([0-9][0-9](0[48]|[2468][048]|[13579][26])|([02468][048]|[13579][26])00)"
_ISO_INSTANT_PATTERN = (
    rf"^(?!0000)(?!(?!{_LEAP_YEAR}-)[0-9]{{4}}-02-29)[0-9]{{4}}"
    r"-(?!02-3|(04|06|09|11)-31)"
    r"(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
    r"[T ]([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9](\.[0-9]+)?)?"
    r"(Z|[+-](0[0-9]|1[0-5])(:?[0-5][0-9])?)?$"
)


def is_text_type(column_type: str | None) -> bool:
    """Whether a catalogue type (``format_type``) is a text type."""
    return column_type is not None and column_type.lower().startswith(_TEXT_TYPES)


def window_instant_sql(quoted: str, column_type: str | None) -> str:
    """The SQL expression that reads a freshness column as an instant.

    A timestamp column is its own instant. A text column (``metering_summary.as_of``
    holds ISO-8601 text) is cast, and a value that is not an ISO-8601 instant is
    NULL, so the comparison drops it from the window; the read names those rows
    in ``window.undated_rows`` instead of failing (OMN-21288).
    """
    if not is_text_type(column_type):
        return quoted
    return (
        f"(CASE WHEN {quoted} ~ '{_ISO_INSTANT_PATTERN}' "
        f"THEN {quoted}::timestamptz END)"
    )


async def freshness_column_type(
    connection: Any, relation: str, column: str
) -> str | None:
    """The catalogue type (``format_type``) of one column of ``relation``, or None."""
    found = await connection.fetchval(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = to_regclass($1) AND attname = $2 AND NOT attisdropped",
        relation,
        column.strip('"'),
    )
    return found if isinstance(found, str) else None


def undated_rows_sql(
    relation: str, quoted_column: str, column_type: str | None, tenant_where: str
) -> str | None:
    """The count of rows a window leaves out because the column is not an instant.

    None for a column that is itself a timestamp: it has no such rows.
    """
    if not is_text_type(column_type):
        return None
    instant = window_instant_sql(quoted_column, column_type)
    joiner = " AND" if tenant_where else " WHERE"
    return f"SELECT count(*) FROM {relation}{tenant_where}{joiner} {instant} IS NULL"


def log_undated_rows(relation: str, column: str, count: int) -> None:
    """Name the rows a window read left out, so they are never dropped silently."""
    if count:
        log.warning(
            "projection window read of %s leaves out %d row(s) whose %s is not an ISO-8601 instant",
            relation,
            count,
            column,
        )
