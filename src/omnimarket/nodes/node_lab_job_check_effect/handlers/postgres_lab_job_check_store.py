# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Postgres read store of the lab job check effect: SELECT statements only.

Every statement goes through :class:`SelectOnlyConnection`, which refuses
anything that is not a single ``SELECT`` before it reaches the driver, and the
connection is held in a read-only transaction so the database refuses a write
too. The pool is the shared ``AsyncpgAdapter``'s, whose DSN is the one the
runtime binds. There is no write method on the store and no parameter that adds one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.models.lab_job import ModelLabJobRow, ModelLabJobSpec
from omnimarket.models.lab_job.model_lab_job_check import ModelLabJobPrObservation
from omnimarket.nodes.node_lab_job_check_effect.handlers.lane_evidence import (
    activity_event_types,
)
from omnimarket.nodes.node_lab_job_check_effect.models import (
    ModelLaneActivityReading,
    ModelLedgerRowReading,
    ModelRelayReading,
)
from omnimarket.projection.tenant_isolation import TENANT_GUC, resolve_rls_read_tenant

_WRITE_WORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|CALL|DO|"
    r"MERGE|VACUUM|REINDEX|LOCK|SET|RESET|NOTIFY|LISTEN)\b",
    re.IGNORECASE,
)


class StatementRefusedError(ValueError):
    """The check effect was asked to run a statement that is not a SELECT."""


def assert_select_only(sql: str) -> None:
    """Raise :class:`StatementRefusedError` unless *sql* is one plain ``SELECT``."""
    text = sql.strip()
    if not text.upper().startswith("SELECT"):
        raise StatementRefusedError(f"not a SELECT: {text[:40]!r}")
    if ";" in text.rstrip(";"):
        raise StatementRefusedError("more than one statement")
    banned = _WRITE_WORDS.search(text)
    if banned is not None:
        raise StatementRefusedError(
            f"write keyword {banned.group(1).upper()} in statement"
        )


class SelectOnlyConnection:
    """Wraps a connection so that only SELECT statements reach it."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def fetch(self, sql: str, *args: object) -> list[Any]:
        assert_select_only(sql)
        return list(await self._conn.fetch(sql, *args))

    async def fetchrow(self, sql: str, *args: object) -> Any:
        assert_select_only(sql)
        return await self._conn.fetchrow(sql, *args)


_JOBS_SQL = """
SELECT job_id, kind, state, episode, attempt, seq, entered_state_at, claimed_at,
       next_dispatch_at, owner_runtime, work_unit_id, run_id, last_verdict,
       last_outcome, time_box_hit, continuation, failure_reason, parent_lane,
       ticket, alert_sent_at, spec
  FROM omninode_internal.lab_job_state
 WHERE state NOT IN ('done', 'alerted')
 ORDER BY job_id
"""

# A CLAIM names its run in a `run=<id>` cell; the cell is matched whole.
_CLAIM_BY_RUN_SQL = """
SELECT row_id, row_ts, row_lane, raw_row
  FROM omninode_internal.work_ledger_rows
 WHERE row_type = 'CLAIM'
   AND ($1::text) = ANY (string_to_array(raw_row, ' | '))
 ORDER BY row_ts DESC
 LIMIT 1
"""

# A runless CLAIM is found by the ticket it names and the time it was written.
_CLAIM_BY_TIME_SQL = """
SELECT row_id, row_ts, row_lane, raw_row
  FROM omninode_internal.work_ledger_rows
 WHERE row_type = 'CLAIM'
   AND row_ts = $1
   AND tickets @> to_jsonb($2::text)
 ORDER BY row_id
 LIMIT 1
"""

_TERMINAL_SQL = """
SELECT row_id, row_ts, row_lane, raw_row
  FROM omninode_internal.work_ledger_rows
 WHERE row_type = 'TERMINAL'
   AND row_lane = $1
   AND row_ts > $2
 ORDER BY row_ts DESC
 LIMIT 1
"""

# Attribution per lane, since that lane's own CLAIM: the key is payload->>'lane'.
_ACTIVITY_SQL = """
SELECT h.payload->>'lane' AS lane,
       MAX(h.occurred_at) FILTER (WHERE h.event_type = ANY($3::text[])) AS last_event_at,
       COUNT(*) FILTER (WHERE h.event_type = ANY($3::text[])) AS event_count,
       COUNT(*) AS attributed_count
  FROM public.hook_events h
  JOIN unnest($1::text[], $2::timestamptz[]) AS c(lane, claimed_at)
    ON h.payload->>'lane' = c.lane
   AND h.occurred_at >= c.claimed_at
 WHERE h.occurred_at < $4
 GROUP BY 1
"""

_RELAY_SQL = """
SELECT MAX(occurred_at) AS last_event_at, COUNT(*) AS event_count
  FROM public.hook_events
 WHERE occurred_at >= $1 AND occurred_at < $2
"""

# pr_state.repo may be bare or owner-qualified; compare the bare name.
_PR_SQL = """
SELECT p.repo, p.pr_number, p.state, p.head_sha, p.ci_verdict, p.red_contexts,
       p.pending_contexts, p.merged_at, p.observed_at
  FROM omninode_internal.pr_state p
  JOIN unnest($1::text[], $2::integer[]) AS t(repo, pr_number)
    ON regexp_replace(p.repo, '^.*/', '') = t.repo
   AND p.pr_number = t.pr_number
"""


def parse_pr_target(target: str) -> tuple[str, int]:
    """``owner/name#n`` or ``name#n`` to ``(name, n)``."""
    repo, _, number = target.rpartition("#")
    if not repo or not number.isdigit():
        raise ValueError(f"pull request target must be repo#n, got {target!r}")
    return repo.rsplit("/", 1)[-1], int(number)


def _contexts(raw: object) -> tuple[str, ...]:
    value = json.loads(raw) if isinstance(raw, str) else raw
    return tuple(str(item) for item in value) if isinstance(value, list) else ()


def _ledger_row(row: Any) -> ModelLedgerRowReading:
    return ModelLedgerRowReading(
        row_id=row["row_id"],
        row_ts=row["row_ts"],
        row_lane=row["row_lane"],
        raw_row=row["raw_row"],
    )


def _job_row(row: Any) -> ModelLabJobRow:
    spec_raw = row["spec"]
    spec = (
        ModelLabJobSpec.model_validate(
            json.loads(spec_raw) if isinstance(spec_raw, str) else spec_raw
        )
        if spec_raw is not None
        else None
    )
    fields = {
        name: row[name]
        for name in [
            "job_id",
            "kind",
            "state",
            "episode",
            "attempt",
            "seq",
            "entered_state_at",
            "claimed_at",
            "next_dispatch_at",
            "owner_runtime",
            "work_unit_id",
            "run_id",
            "last_verdict",
            "last_outcome",
            "time_box_hit",
            "continuation",
            "failure_reason",
            "parent_lane",
            "ticket",
            "alert_sent_at",
        ]
    }
    return ModelLabJobRow(spec=spec, **fields)


class PostgresLabJobCheckStore:
    """Reads the job table, the ledger projection, hook events and PR state."""

    def __init__(self, database: AsyncpgAdapter) -> None:
        self._db = database

    @classmethod
    async def connect_default(cls) -> PostgresLabJobCheckStore:
        """Open the shared adapter on the projection database the runtime binds."""
        database = AsyncpgAdapter(min_size=1, max_size=2)
        await database.connect()
        return cls(database)

    async def _read(self, sql: str, *args: object) -> list[Any]:
        # hook_events is RLS-covered: the tenant is resolved before any statement
        # and set in the same read-only transaction as the read.
        tenant = resolve_rls_read_tenant(None, table="hook_events")
        async with self._db.pool.acquire() as raw, raw.transaction(readonly=True):
            conn = SelectOnlyConnection(raw)
            await conn.fetch("SELECT set_config($1, $2, true)", TENANT_GUC, tenant)
            return await conn.fetch(sql, *args)

    async def open_jobs(self) -> tuple[ModelLabJobRow, ...]:
        return tuple(_job_row(row) for row in await self._read(_JOBS_SQL))

    async def claim_row(self, job: ModelLabJobRow) -> ModelLedgerRowReading | None:
        rows: list[Any] = []
        if job.run_id:
            rows = await self._read(_CLAIM_BY_RUN_SQL, f"run={job.run_id}")
        if not rows and job.claimed_at is not None and job.ticket:
            rows = await self._read(_CLAIM_BY_TIME_SQL, job.claimed_at, job.ticket)
        return _ledger_row(rows[0]) if rows else None

    async def terminal_row_after(
        self, lane: str, claimed_at: datetime
    ) -> ModelLedgerRowReading | None:
        rows = await self._read(_TERMINAL_SQL, lane, claimed_at)
        return _ledger_row(rows[0]) if rows else None

    async def lane_activity(
        self, claims: Mapping[str, datetime], until: datetime
    ) -> Mapping[str, ModelLaneActivityReading]:
        if not claims:
            return {}
        lanes = sorted(claims)
        rows = await self._read(
            _ACTIVITY_SQL,
            lanes,
            [claims[lane] for lane in lanes],
            list(activity_event_types()),
            until,
        )
        return {
            row["lane"]: ModelLaneActivityReading(
                lane=row["lane"],
                last_event_at=row["last_event_at"],
                event_count=int(row["event_count"]),
                attributed_count=int(row["attributed_count"]),
            )
            for row in rows
        }

    async def relay(self, since: datetime, until: datetime) -> ModelRelayReading:
        rows = await self._read(_RELAY_SQL, since, until)
        row = rows[0]
        return ModelRelayReading(
            last_event_at=row["last_event_at"], event_count=int(row["event_count"])
        )

    async def pr_states(
        self, targets: Sequence[str]
    ) -> Mapping[str, ModelLabJobPrObservation]:
        wanted = sorted(set(targets))
        if not wanted:
            return {}
        parsed = [parse_pr_target(target) for target in wanted]
        rows = await self._read(
            _PR_SQL, [name for name, _ in parsed], [number for _, number in parsed]
        )
        by_key = {
            (row["repo"].rsplit("/", 1)[-1], int(row["pr_number"])): row for row in rows
        }
        found: dict[str, ModelLabJobPrObservation] = {}
        for target, key in zip(wanted, parsed, strict=True):
            row = by_key.get(key)
            if row is None:
                continue
            found[target] = ModelLabJobPrObservation(
                target=target,
                found=True,
                state=row["state"],
                head_sha=row["head_sha"],
                ci_verdict=row["ci_verdict"],
                red_contexts=_contexts(row["red_contexts"]),
                pending_contexts=_contexts(row["pending_contexts"]),
                merged_at=row["merged_at"] or None,
                read_at=row["observed_at"],
            )
        return found


__all__: list[str] = [
    "PostgresLabJobCheckStore",
    "SelectOnlyConnection",
    "StatementRefusedError",
    "assert_select_only",
    "parse_pr_target",
]
