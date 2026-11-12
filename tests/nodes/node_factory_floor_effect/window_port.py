# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A read port that answers the work ledger query node's rows query over recorded ledger lines (OMN-23669).

It filters as the node does (kinds, a case-insensitive term in the first line, the half-open window) and pages by an
offset cursor, so a test of the floor effect reads recorded rows through the same request the publisher sends.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    ModelPointAnswer,
    ModelPointQuery,
)

_ROW = re.compile(r"^(?:\S+?:)?L\d+ (?P<row>.*)$")


def _row(line: str) -> str:
    match = _ROW.match(line)
    return match.group("row") if match else line


def _stamp(row: str) -> datetime:
    return datetime.strptime(row.split(" | ")[0], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )


def _kind(row: str) -> str:
    return row.split(" | ")[1]


class RecordedWindowPort:
    """Serves recorded ledger text; ``requests`` keeps every query it was sent and ``fail_on`` names a kind that errors."""

    def __init__(self, text: str, *, fail_on: str | None = None) -> None:
        self._rows = [_row(line) for line in text.splitlines() if line.strip()]
        self._fail_on = fail_on
        self.requests: list[ModelPointQuery] = []

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        self.requests.append(query)
        if self._fail_on is not None and self._fail_on in query.kinds:
            return ModelPointAnswer(error="node-error:refused")
        found = [
            row
            for row in self._rows
            if (not query.kinds or _kind(row) in query.kinds)
            and (query.term is None or query.term.casefold() in row.casefold())
            and (query.since is None or _stamp(row) >= query.since)
            and (query.until is None or _stamp(row) < query.until)
        ]
        start = int(query.cursor or 0)
        size = query.page_size or len(found) or 1
        end = start + size
        return ModelPointAnswer(
            lines=tuple(found[start:end]),
            matched=len(found),
            next_cursor=str(end) if end < len(found) else None,
        )
