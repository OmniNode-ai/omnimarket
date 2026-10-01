# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Work-ledger projection: the pure fold and its effect-class writer (OMN-19513).

Rule 7a, two classes. ``HandlerProjectionWorkLedger`` is the definition-B fold
(typed in, typed out, no clock, no broker, no database).
``WorkLedgerProjectionWriter`` is the entry the runtime actually calls: it takes
the injected database handle, calls the fold and persists its result. It holds
no business logic of its own, and it declares in-process dispatch. Get either
half wrong and the symptom is identical: offsets commit, the table has zero rows
and nothing raises.

Ordering is enforced in SQL. Each state write touches only its own column group
under an ``ON CONFLICT ... WHERE (at, row_id) <= EXCLUDED`` guard, so a
redelivered or reordered event cannot move an entity backwards.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import yaml

from omnimarket.nodes.node_projection_work_ledger.contract_topics import (
    SUBSCRIBE_TOPICS,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_write_guard import (
    HandlerWorkLedgerWriteGuard,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_fold import (
    WorkLedgerFoldError,
    fold_row,
)
from omnimarket.nodes.node_projection_work_ledger.models.enum_work_ledger_entity_kind import (
    EnumWorkLedgerStateOp,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_fold_request import (
    ModelWorkLedgerFoldRequest,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_fold_result import (
    ModelWorkLedgerFoldResult,
    ModelWorkLedgerStateOp,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

ROWS_TABLE = "omninode_internal.work_ledger_rows"
STATE_TABLE = "omninode_internal.work_ledger_state"

_INSERT_ROW = f"""
    INSERT INTO {ROWS_TABLE}
        (row_id, ledger_id, row_ts, row_type, row_lane, tickets, raw_row, source, projected_at)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
    ON CONFLICT (row_id) DO NOTHING
"""

_OPEN_ENTITY = f"""
    INSERT INTO {STATE_TABLE}
        (entity_key, kind, lane, ticket, repo, pr, scope_to, scope_surface, until_at,
         detail, opened_at, opened_row_id, projected_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
    ON CONFLICT (entity_key) DO UPDATE SET
        lane = EXCLUDED.lane, ticket = EXCLUDED.ticket, repo = EXCLUDED.repo,
        pr = EXCLUDED.pr, scope_to = EXCLUDED.scope_to,
        scope_surface = EXCLUDED.scope_surface, until_at = EXCLUDED.until_at,
        detail = EXCLUDED.detail, opened_at = EXCLUDED.opened_at,
        opened_row_id = EXCLUDED.opened_row_id, projected_at = EXCLUDED.projected_at
    WHERE {STATE_TABLE}.opened_at IS NULL
       OR ({STATE_TABLE}.opened_at, {STATE_TABLE}.opened_row_id)
          <= (EXCLUDED.opened_at, EXCLUDED.opened_row_id)
"""

_CLOSE_ENTITY = f"""
    INSERT INTO {STATE_TABLE} (entity_key, kind, closed_at, closed_row_id, projected_at)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (entity_key) DO UPDATE SET
        closed_at = EXCLUDED.closed_at, closed_row_id = EXCLUDED.closed_row_id,
        projected_at = EXCLUDED.projected_at
    WHERE {STATE_TABLE}.closed_at IS NULL
       OR ({STATE_TABLE}.closed_at, {STATE_TABLE}.closed_row_id)
          <= (EXCLUDED.closed_at, EXCLUDED.closed_row_id)
"""

_T = TypeVar("_T")


class HandlerProjectionWorkLedger:
    """The PURE half: one row event in, its log record and state ops out."""

    def handle(self, request: ModelWorkLedgerFoldRequest) -> ModelWorkLedgerFoldResult:
        return fold_row(request)


def _op_args(op: ModelWorkLedgerStateOp, now: datetime) -> tuple[str, tuple[Any, ...]]:
    if op.op is EnumWorkLedgerStateOp.OPEN:
        return _OPEN_ENTITY, (
            op.entity_key,
            op.kind.value,
            op.lane,
            op.ticket,
            op.repo,
            op.pr,
            op.scope_to,
            op.scope_surface,
            op.until_at,
            op.detail,
            op.at,
            op.row_id,
            now,
        )
    return _CLOSE_ENTITY, (op.entity_key, op.kind.value, op.at, op.row_id, now)


class WorkLedgerProjectionWriter(BaseProjectionRunner):
    """The DURABLE half: calls the fold and persists its result."""

    #: In-process dispatch (rule 7a, trap two). The pool is opened and closed on
    #: the loop each message runs on, which is the lifetime the runtime can honour.
    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._dispatch_lock = threading.Lock()

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def subscribe_topics(self) -> list[str]:
        declared = list(self._contract.get("event_bus", {}).get("subscribe_topics", []))
        if sorted(declared) != sorted(SUBSCRIBE_TOPICS):
            raise WorkLedgerFoldError(
                f"contract subscribe_topics disagree with contract_topics: {declared}"
            )
        return declared

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal projection shim: one injected message, one write."""
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
            topic=topic,
        )
        with self._dispatch_lock:
            written = self._run(self._project_one_message(topic, input_data, meta))
        # ``rows_upserted`` is the key the runtime's write-path guard reads.
        return {"rows_upserted": written}

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> int:
        self._refuse_real_dsn_under_test()
        try:
            await self.db.connect()
            return await self._project_and_report(topic, data, meta)
        finally:
            await self._stop_producer()
            await self.db.close()

    @staticmethod
    def _run(coro: Coroutine[Any, Any, _T]) -> _T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    def _refuse_real_dsn_under_test(self) -> None:
        """Judged before any connect or statement: a test process never writes the real DSN."""
        HandlerWorkLedgerWriteGuard.check_dsn(str(getattr(self._db, "dsn", "") or ""))

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_and_report(topic, data, meta)
        return True

    async def _project_and_report(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> int:
        """Fold, then write the log record and each state op. Returns rows written."""
        self._refuse_real_dsn_under_test()
        if topic not in SUBSCRIBE_TOPICS:
            raise WorkLedgerFoldError(f"unsubscribed topic {topic!r}")
        result = fold_row(ModelWorkLedgerFoldRequest.model_validate(data))
        now = datetime.now(UTC)
        row = result.row
        await self._db.execute(
            _INSERT_ROW,
            row.row_id,
            row.ledger_id,
            row.row_ts,
            row.row_type,
            row.row_lane,
            json.dumps(list(row.tickets)),
            row.raw_row,
            row.source,
            now,
        )
        for op in result.ops:
            sql, args = _op_args(op, now)
            await self._db.execute(sql, *args)
        return 1 + len(result.ops)


__all__: list[str] = ["HandlerProjectionWorkLedger", "WorkLedgerProjectionWriter"]
