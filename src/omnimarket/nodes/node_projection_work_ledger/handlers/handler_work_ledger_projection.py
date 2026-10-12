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
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    import asyncpg

import yaml
from omnibase_core.enums.enum_question_status import EnumQuestionStatus
from omnibase_core.models.events.work.model_work_ledger_line import (
    dump_work_ledger_line,
    parse_work_ledger_line,
)
from omnibase_core.models.events.work.model_work_ledger_render import ROW_TYPE_BY_KIND

from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
from omnimarket.events.model_ledger_row_event import (
    work_ledger_event_id,
    work_ledger_row_id,
)
from omnimarket.nodes.node_projection_work_ledger.contract_topics import (
    ALL_SUBSCRIBE_TOPICS,
    SUBSCRIBE_TOPICS,
    TYPED_SUBSCRIBE_TOPICS,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_write_guard import (
    HandlerWorkLedgerWriteGuard,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_fold import (
    WorkLedgerFoldError,
    fold_records,
    fold_row,
    rows_for_records,
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
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_projection import (
    ModelWorkLedgerProjectionConfig,
    ModelWorkLedgerProjectionInbound,
    ModelWorkLedgerProjectionRequest,
    ModelWorkLedgerProjectionResult,
)
from omnimarket.projection.envelope import strip_runner_injected_keys
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

ROWS_TABLE = "omninode_internal.work_ledger_rows"
STATE_TABLE = "omninode_internal.work_ledger_state"

# A row first projected by an older emitter or the emit backfill gains its seq
# when the sequenced event arrives. A stored seq is immutable, never overwritten.
_INSERT_ROW = f"""
    INSERT INTO {ROWS_TABLE}
        (row_id, ledger_id, row_ts, row_type, row_lane, tickets, raw_row, source,
         projected_at, ledger_seq)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10)
    ON CONFLICT (row_id) DO UPDATE SET ledger_seq = EXCLUDED.ledger_seq
    WHERE {ROWS_TABLE}.ledger_seq IS NULL AND EXCLUDED.ledger_seq IS NOT NULL
"""

_ADVISORY_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))"

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

_READ_ROWS = f"SELECT row_id, ledger_id, raw_row, event_id, record, provenance_kind FROM {ROWS_TABLE} ORDER BY row_id"
_BACKFILL_ROW = f"""
    UPDATE {ROWS_TABLE} SET event_id = $2
    WHERE row_id = $1 AND (event_id IS NULL OR event_id = $2)
    RETURNING row_id
"""
_INSERT_TYPED_ROW = f"""
    INSERT INTO {ROWS_TABLE}
        (row_id, ledger_id, row_ts, row_type, row_lane, tickets, raw_row, source,
         projected_at, event_id, record, provenance_kind)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, $12)
    ON CONFLICT (row_id) DO UPDATE SET
        event_id = EXCLUDED.event_id,
        record = COALESCE({ROWS_TABLE}.record, EXCLUDED.record),
        provenance_kind = COALESCE({ROWS_TABLE}.provenance_kind, EXCLUDED.provenance_kind)
    WHERE {ROWS_TABLE}.ledger_id = EXCLUDED.ledger_id
      AND {ROWS_TABLE}.raw_row = EXCLUDED.raw_row
      AND ({ROWS_TABLE}.event_id IS NULL OR {ROWS_TABLE}.event_id = EXCLUDED.event_id)
      AND ({ROWS_TABLE}.record IS NULL OR {ROWS_TABLE}.record = EXCLUDED.record)
      AND ({ROWS_TABLE}.provenance_kind IS NULL OR {ROWS_TABLE}.provenance_kind = EXCLUDED.provenance_kind)
    RETURNING row_id, event_id, record
"""
_UPSERT_QUESTION = f"""
    INSERT INTO {STATE_TABLE}
        (entity_key, kind, lane, detail, opened_at, opened_row_id,
         projected_at, question_status, question_event, record)
    VALUES ($1, 'question', $2, $3, $4, $5, $6, $7, $8, $9)
    ON CONFLICT (entity_key) DO UPDATE SET
        question_status = EXCLUDED.question_status,
        record = EXCLUDED.record, projected_at = EXCLUDED.projected_at
    WHERE CASE {STATE_TABLE}.question_status
          WHEN '{EnumQuestionStatus.ANSWERED.value}' THEN 2 WHEN '{EnumQuestionStatus.WITHDRAWN.value}' THEN 1 ELSE 0 END
       <= CASE EXCLUDED.question_status
          WHEN '{EnumQuestionStatus.ANSWERED.value}' THEN 2 WHEN '{EnumQuestionStatus.WITHDRAWN.value}' THEN 1 ELSE 0 END
"""

_T = TypeVar("_T")


class HandlerProjectionWorkLedger:
    """The PURE half: one row event in, its log record and state ops out."""

    rows_for_records = staticmethod(rows_for_records)
    fold_records = staticmethod(fold_records)

    def handle_records(
        self, request: ModelWorkLedgerProjectionRequest
    ) -> ModelWorkLedgerProjectionResult:
        """Pure typed projection entrypoint; core owns all state semantics."""
        return ModelWorkLedgerProjectionResult(
            rows=rows_for_records(request.records),
            state=fold_records(request.records, as_of=request.as_of),
        )

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
        self._config = ModelWorkLedgerProjectionConfig.model_validate(
            self._contract["work_ledger"]
        )
        self._dispatch_lock = threading.Lock()

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def subscribe_topics(self) -> list[str]:
        declared = list(self._contract.get("event_bus", {}).get("subscribe_topics", []))
        if sorted(declared) != sorted(ALL_SUBSCRIBE_TOPICS):
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
        if topic in TYPED_SUBSCRIBE_TOPICS:
            return await self._project_typed(topic, data)
        if topic not in SUBSCRIBE_TOPICS:
            raise WorkLedgerFoldError(f"unsubscribed topic {topic!r}")
        request = ModelWorkLedgerFoldRequest.model_validate(data)
        if request.ledger_id != self._config.ledger_id:
            raise WorkLedgerFoldError("configured ledger_id mismatch")
        result = fold_row(request)
        expected_id = str(work_ledger_event_id(result.row.ledger_id, result.row.row_id))
        if data.get("event_id") is not None and str(data["event_id"]) not in {
            expected_id,
            result.row.row_id,
        }:
            raise WorkLedgerFoldError(
                "legacy event_id must match canonical UUID5 or exact historical source hash"
            )
        now = datetime.now(UTC)
        row = result.row
        async with self._db.pool.acquire() as conn, conn.transaction():
            await conn.execute(_ADVISORY_LOCK, f"work-ledger:{request.ledger_id}")
            await conn.execute(
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
                row.ledger_seq,
            )
            for op in result.ops:
                sql, args = _op_args(op, now)
                await conn.execute(sql, *args)
        return 1 + len(result.ops)

    def _validated_rows(
        self, rows: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], Any]]:
        """Validate the complete legacy grain before issuing any backfill write."""
        validated = []
        for row in rows:
            if row["ledger_id"] != self._config.ledger_id:
                raise WorkLedgerFoldError(
                    "configured ledger_id mismatch during reconciliation"
                )
            if (
                row["raw_row"] != row["raw_row"].strip()
                or work_ledger_row_id(row["raw_row"]) != row["row_id"]
            ):
                raise WorkLedgerFoldError(
                    "changed normalized source row during reconciliation"
                )
            if row["record"] is None:
                event_id = work_ledger_event_id(row["ledger_id"], row["row_id"])
            else:
                record = parse_work_ledger_line(row["record"])
                if dump_work_ledger_line(record) != row["record"]:
                    raise WorkLedgerFoldError("noncanonical stored core record")
                event_id = record.event.event_id
                if row[
                    "provenance_kind"
                ] == "markdown" and event_id != work_ledger_event_id(
                    row["ledger_id"], row["row_id"]
                ):
                    raise WorkLedgerFoldError("stored markdown event_id mismatch")
                if row["provenance_kind"] not in {"markdown", "typed"}:
                    raise WorkLedgerFoldError(
                        "stored core record has no typed provenance"
                    )
            if row["event_id"] is not None and str(row["event_id"]) != str(event_id):
                raise WorkLedgerFoldError("stored row_id/event_id mismatch")
            validated.append((row, event_id))
        return validated

    async def _project_typed(self, topic: str, data: dict[str, Any]) -> int:
        inbound = ModelWorkLedgerProjectionInbound.model_validate(
            strip_runner_injected_keys(data)
        )
        if inbound.ledger_id != self._config.ledger_id:
            raise WorkLedgerFoldError("configured ledger_id mismatch")
        row_type = EnumLedgerRowType(ROW_TYPE_BY_KIND[inbound.event.kind])
        if topic != row_type.typed_topic:
            raise WorkLedgerFoldError("typed work-ledger topic/type mismatch")
        async with self._db.pool.acquire() as conn, conn.transaction():
            # All writers for this configured ledger see a serial canonical set.
            await conn.execute(_ADVISORY_LOCK, f"work-ledger:{inbound.ledger_id}")
            return await self._persist_typed(conn, inbound, row_type)

    async def _reconcile_rows(self, conn: asyncpg.Connection) -> None:
        prior_rows = [dict(row) for row in await conn.fetch(_READ_ROWS)]
        for row, event_id in self._validated_rows(prior_rows):
            if row["event_id"] is None:
                backfilled = await conn.fetch(_BACKFILL_ROW, row["row_id"], event_id)
                if not backfilled:
                    raise WorkLedgerFoldError("concurrent reconciliation conflict")

    async def _persist_typed(
        self,
        conn: asyncpg.Connection,
        inbound: ModelWorkLedgerProjectionInbound,
        row_type: EnumLedgerRowType,
    ) -> int:
        await self._reconcile_rows(conn)
        record = inbound.to_record()
        canonical = dump_work_ledger_line(record)
        returned = await conn.fetch(
            _INSERT_TYPED_ROW,
            inbound.row_id,
            inbound.ledger_id,
            inbound.event.emitted_at,
            row_type.value,
            inbound.event.actor.actor_key,
            json.dumps([inbound.event.ticket_id] if inbound.event.ticket_id else []),
            inbound.raw_row,
            inbound.source,
            datetime.now(UTC),
            inbound.event_id,
            canonical,
            inbound.provenance_kind,
        )
        if not returned:
            raise WorkLedgerFoldError(
                "conflicting immutable row_id/provenance/canonical record"
            )
        records = [
            parse_work_ledger_line(row["record"])
            for row in await conn.fetch(_READ_ROWS)
            if row["record"] is not None
        ]
        state = fold_records(records)
        for question in state.questions:
            await conn.execute(
                _UPSERT_QUESTION,
                f"question:{question.question.event_id}",
                question.question.actor.actor_key,
                question.question.question,
                question.question.emitted_at,
                str(question.question.event_id),
                datetime.now(UTC),
                question.status.value,
                question.question.event_id,
                question.model_dump_json(),
            )
        return len(returned) + len(state.questions)


__all__: list[str] = ["HandlerProjectionWorkLedger", "WorkLedgerProjectionWriter"]
