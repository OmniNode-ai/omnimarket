# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the live cause CLAIMs that own a dev-head red from the database work ledger (OMN-20730).

A cause lane started by the remote-lane runner carries ``cause=<repo>:<check>`` on its CLAIM;
the landing controller's cause lease is a relay CLAIM ``cause=<cause key> | check=<checks>``.
Both rows land in ``omninode_internal.work_ledger_rows``. This reader takes the CLAIM and TERMINAL
rows of the lease window and leaves the owner rule to ``live_cause_owner``. It never writes. Every
read failure raises, so an unreadable ledger withholds the request instead of reading as unowned.
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Protocol

from omnimarket.models.dev_head_red import live_cause_owner
from omnimarket.models.work_ledger_query import ModelWorkLedgerRowRecord

_RELATION_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")


class DevCauseOwnersUnreadError(RuntimeError):
    """The work ledger could not be read; a cause-lane request must be withheld."""


class ProtocolDevCauseOwners(Protocol):
    def owner(self, repo: str, check: str, now: datetime) -> str | None: ...


class UnboundDevCauseOwners:
    """No work ledger is bound (its DSN variable is unset): every read is unread."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def owner(self, repo: str, check: str, now: datetime) -> str | None:
        raise DevCauseOwnersUnreadError(self._reason)


class WorkLedgerDevCauseOwners:
    """CLAIM and TERMINAL rows of the lease window, read through one read-only connection."""

    def __init__(self, relation: str, dsn: str, live: timedelta) -> None:
        if not _RELATION_RE.match(relation):
            raise ValueError(f"relation must be <schema>.<table>, got {relation!r}")
        self._relation = relation
        self._dsn = dsn
        self._live = live
        self._conn: Any | None = None
        self._lock = threading.Lock()

    def _rows(self, since: datetime, until: datetime) -> list[tuple[Any, ...]]:
        from omnimarket.projection.postgres_read_database import connect_read_only

        if self._conn is None or self._conn.closed:
            self._conn = connect_read_only(self._dsn)
        # The relation is a contract value matched against _RELATION_RE above.
        query = (
            "SELECT row_id, row_ts, row_type, row_lane, raw_row, source "
            f"FROM {self._relation} WHERE row_type IN ('CLAIM', 'TERMINAL') "
            "AND row_ts >= %s AND row_ts <= %s ORDER BY row_ts, row_id"
        )
        with self._conn.cursor() as cursor:
            cursor.execute(query, (since, until))
            return list(cursor.fetchall())

    def owner(self, repo: str, check: str, now: datetime) -> str | None:
        try:
            with self._lock:
                raw = self._rows(now - self._live, now)
        except Exception as exc:
            with contextlib.suppress(Exception):
                if self._conn is not None:
                    self._conn.close()
            self._conn = None
            raise DevCauseOwnersUnreadError(type(exc).__name__) from None
        rows = [
            ModelWorkLedgerRowRecord(
                row_id=str(row_id),
                row_ts=row_ts,
                row_type=str(row_type),
                row_lane=row_lane,
                text=str(raw_row).split("\n", 1)[0],
                raw_row=str(raw_row),
                source=str(source),
            )
            for row_id, row_ts, row_type, row_lane, raw_row, source in raw
        ]
        return live_cause_owner(rows, repo=repo, check=check, now=now, live=self._live)


def dev_cause_owners_from_contract(
    block: dict[str, object], live: timedelta | None = None
) -> ProtocolDevCauseOwners:
    """Bind the read-only work ledger reader the contract's owners block names."""
    if set(block) != {"relation", "dsn_env"}:
        raise ValueError(
            f"owners must declare exactly relation and dsn_env, got {sorted(block)}"
        )
    dsn_env = str(block["dsn_env"])
    dsn = os.environ.get(dsn_env, "").strip()
    if not dsn:
        return UnboundDevCauseOwners(f"{dsn_env} unset")
    return WorkLedgerDevCauseOwners(
        str(block["relation"]),
        dsn,
        live if live is not None else timedelta(seconds=5400),
    )
