# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Held DoD outcome join for the Postgres reader (OMN-26152).

The routing consumers read the (task type, tenant) join once per delegation, and
the unchanged answer was fetched, built into dicts and joined every time. This
holder keeps the joined rows and re-reads only when a cheap revision probe of
either source (row count, newest timestamp, decided count) has moved, so the
answer always equals a fresh read.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

HELD_JOIN_LIMIT = 64

Row = dict[str, object]
JoinFn = Callable[[list[Row], list[Row]], list[Row]]


class HeldDodOutcomeJoin:
    def __init__(
        self,
        *,
        get_conn: Callable[[str], Any],
        delegation_dsn: str,
        verdict_dsn: str,
        verdict_sql: str,
        delegation_sql: str,
        verdict_revision_sql: str,
        delegation_revision_sql: str,
        tenant_guc: str,
        join: JoinFn,
    ) -> None:
        self._get_conn = get_conn
        self._delegation_dsn = delegation_dsn
        self._verdict_dsn = verdict_dsn
        self._verdict_sql = verdict_sql
        self._delegation_sql = delegation_sql
        self._verdict_revision_sql = verdict_revision_sql
        self._delegation_revision_sql = delegation_revision_sql
        self._tenant_guc = tenant_guc
        self._join = join
        self._held: dict[tuple[str, str], tuple[tuple[object, ...], list[Row]]] = {}

    def _read_verdicts(self, verdict_conn: Any) -> list[Row]:
        extras = importlib.import_module("psycopg2.extras")

        with verdict_conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            cur.execute(self._verdict_sql)
            return [dict(row) for row in cur.fetchall()]

    def read(self, *, task_type: str, tenant_id: str) -> list[Row]:
        extras = importlib.import_module("psycopg2.extras")

        key = (task_type, tenant_id)
        verdict_conn = self._get_conn(self._verdict_dsn)
        with verdict_conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            cur.execute(self._verdict_revision_sql)
            probe = cur.fetchone() or {}
        verdict_revision = (
            probe.get("rev_count"),
            probe.get("rev_newest"),
            probe.get("rev_decided"),
        )
        if not probe.get("rev_count"):
            self._held.pop(key, None)
            return []

        conn = self._get_conn(self._delegation_dsn)
        conn.autocommit = False
        try:
            with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT set_config(%s, %s, true)", (self._tenant_guc, tenant_id)
                )
                params = {"task_type": task_type, "tenant_id": tenant_id}
                cur.execute(self._delegation_revision_sql, params)
                dprobe = cur.fetchone() or {}
                revision = (
                    *verdict_revision,
                    dprobe.get("rev_count"),
                    dprobe.get("rev_newest"),
                )
                held = self._held.get(key)
                if held is not None and held[0] == revision:
                    conn.commit()
                    return [dict(row) for row in held[1]]
                verdicts = self._read_verdicts(verdict_conn)
                run_ids = sorted(
                    {
                        str(v["delegation_correlation_id"]).strip().lower()
                        for v in verdicts
                        if v.get("delegation_correlation_id")
                    }
                )
                if not run_ids:
                    conn.commit()
                    return []
                cur.execute(self._delegation_sql, {**params, "run_ids": run_ids})
                delegations = [dict(row) for row in cur.fetchall()]
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
        joined = self._join(delegations, verdicts)
        if len(self._held) >= HELD_JOIN_LIMIT:
            self._held.pop(next(iter(self._held)))
        self._held[key] = (revision, joined)
        return [dict(row) for row in joined]
