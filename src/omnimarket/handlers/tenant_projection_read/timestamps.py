# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The system clock and the ISO-8601 Z rendering the v1 shapes carry."""

from __future__ import annotations

from datetime import UTC, datetime

NOT_CONFIGURED_DETAIL = "delegation analytics source not configured"
UNAVAILABLE_DETAIL = "delegation analytics source unavailable"


class SystemClock:
    """``ProtocolClock`` over the host clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


def iso_z(value: datetime) -> str:
    """A timestamp as UTC with a ``Z`` suffix; a naive value is taken as UTC."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")
