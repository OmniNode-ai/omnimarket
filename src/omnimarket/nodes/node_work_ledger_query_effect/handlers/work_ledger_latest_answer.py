# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The latest-state query's request mapping, cursor and answer (OMN-23606).

One keyset page of the newest row per (subject kind, subject, row type), read
from ``work_ledger_latest``. The cursor names the last entry of a page and the
query it belongs to, the way the point queries' and the rows query's cursors do.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from pathlib import Path

import yaml

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerLatestSubject,
    ModelWorkLedgerLatestCursor,
    ModelWorkLedgerLatestEntry,
    ModelWorkLedgerLatestQuery,
    ModelWorkLedgerQueryRequest,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    WorkLedgerReadError,
)

_QUERY = "latest-state"


def load_latest_default_page_size(contract_path: Path) -> int:
    """The contract's ``work_ledger_query.latest_default_page_size``; a missing or invalid value raises."""
    with contract_path.open(encoding="utf-8") as handle:
        contract = yaml.safe_load(handle)
    value = (contract.get("work_ledger_query") or {}).get("latest_default_page_size")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(
            "work_ledger_query.latest_default_page_size must be a positive integer"
        )
    return value


def encode_latest_cursor(entry: ModelWorkLedgerLatestEntry) -> str:
    """Opaque cursor naming the last entry of a page and the query it belongs to."""
    body = json.dumps(
        {
            "q": _QUERY,
            "k": entry.subject_kind.value,
            "s": entry.subject,
            "t": entry.row_type,
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")


def decode_latest_cursor(cursor: str) -> ModelWorkLedgerLatestCursor:
    try:
        body = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if body["q"] != _QUERY:
            raise WorkLedgerReadError(f"cursor belongs to query {body['q']!r}")
        return ModelWorkLedgerLatestCursor(
            subject_kind=EnumWorkLedgerLatestSubject(body["k"]),
            subject=str(body["s"]),
            row_type=str(body["t"]),
        )
    except WorkLedgerReadError:
        raise
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise WorkLedgerReadError("cursor is not a page cursor of this query") from None


def latest_query_for(
    request: ModelWorkLedgerQueryRequest, *, limit: int
) -> ModelWorkLedgerLatestQuery:
    """The reader's page request for a latest-state request.

    ``tickets`` narrow ticket subjects and ``lanes`` (or ``lane``) narrow lane subjects; naming both
    without a ``subject_kind`` is refused, because one subject list cannot mean two kinds.
    """
    lanes = request.lanes or ((request.lane,) if request.lane else ())
    if lanes and request.tickets:
        raise WorkLedgerReadError(
            "latest-state names lanes or tickets, not both: ask once per subject kind"
        )
    subjects: tuple[str, ...] = lanes or request.tickets
    subject_kind = request.subject_kind
    if subject_kind is None and subjects:
        subject_kind = (
            EnumWorkLedgerLatestSubject.LANE
            if lanes
            else EnumWorkLedgerLatestSubject.TICKET
        )
    since: datetime | None = request.since
    return ModelWorkLedgerLatestQuery(
        limit=limit,
        after=decode_latest_cursor(request.cursor) if request.cursor else None,
        subject_kind=subject_kind,
        subjects=subjects,
        row_types=request.kinds,
        since=since,
    )


__all__: list[str] = [
    "decode_latest_cursor",
    "encode_latest_cursor",
    "latest_query_for",
    "load_latest_default_page_size",
]
