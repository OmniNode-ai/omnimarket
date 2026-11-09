# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The serializable Postgres backend of the gate store (plan section 7.5).

One request is one SERIALIZABLE transaction: the request is applied inside it and the commit
is the last stage, so a kill at ``before-apply``, ``after-apply`` or ``before-commit`` rolls
the transaction back and leaves no trace. A serialization failure, a deadlock or a lost
insert race is retried on a fresh transaction, which re-reads the state, so racing requests
are decided one after another and never both against the same count. The same contract suite
the in-memory twin passes runs unchanged against this backend.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg2  # type: ignore[import-untyped]
import psycopg2.errors  # type: ignore[import-untyped]
import psycopg2.extensions  # type: ignore[import-untyped]

from omnimarket.models.model_gate_store import (
    EnumGateStoreOperation,
    EnumGateStoreOutcome,
    ModelGateStorePolicy,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)

MIGRATION = (
    Path(__file__).resolve().parent.parent / "migrations" / "0000_create_gate_store.sql"
)

_SAFETY_STATES = frozenset({"stop", "clear"})
_SCHEMA = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_RETRIED = (
    psycopg2.errors.SerializationFailure,
    psycopg2.errors.DeadlockDetected,
    psycopg2.errors.UniqueViolation,
)
_MAX_ATTEMPTS = 200


class GateStoreContentionError(RuntimeError):
    """A request lost every one of its attempts to concurrent requests."""


@dataclass
class _Meta:
    epoch: int
    holder: str | None
    order_position: int
    dirty: bool = False


class PostgresGateStore:
    """The gate store on a Postgres schema; a connection is opened per request."""

    def __init__(
        self,
        policy: ModelGateStorePolicy,
        dsn: str,
        schema: str | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        if schema is not None and not _SCHEMA.match(schema):
            raise ValueError(f"gate store schema {schema!r} is not a plain identifier")
        self._policy = policy
        self._dsn = dsn
        self._schema = schema
        self._fault = fault_hook

    def install_schema(self) -> None:
        """Run the node's migration on the configured schema (idempotent)."""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                if self._schema is not None:
                    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {self._schema}")
                cur.execute(MIGRATION.read_text(encoding="utf-8"))
            conn.commit()
        finally:
            conn.close()

    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        """Apply one request in one SERIALIZABLE transaction; a kill leaves no trace."""
        for attempt in range(_MAX_ATTEMPTS):
            conn = self._connect()
            try:
                conn.set_session(
                    isolation_level=psycopg2.extensions.ISOLATION_LEVEL_SERIALIZABLE
                )
                self._at("before-apply")
                with conn.cursor() as cur:
                    result = self._apply(cur, request)
                self._at("after-apply")
                self._at("before-commit")
                conn.commit()
                return result
            except _RETRIED:
                conn.rollback()
                time.sleep(random.uniform(0, 0.005 * (attempt + 1)))
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
        raise GateStoreContentionError(
            f"{request.correlation_id}: {_MAX_ATTEMPTS} attempts lost to concurrent requests"
        )

    def _connect(self) -> Any:
        if self._schema is None:
            return psycopg2.connect(self._dsn)
        return psycopg2.connect(self._dsn, options=f"-c search_path={self._schema}")

    def _at(self, stage: str) -> None:
        if self._fault is not None:
            self._fault(stage)

    @staticmethod
    def _counted(cur: Any, piece: str) -> int:
        cur.execute(
            "SELECT count(*) FROM gate_store_reservation WHERE piece = %s", (piece,)
        )
        return int(cur.fetchone()[0])

    def _answer(
        self,
        cur: Any,
        meta: _Meta,
        request: ModelGateStoreRequest,
        outcome: EnumGateStoreOutcome,
        detail: str = "",
        piece: str | None = None,
    ) -> ModelGateStoreResult:
        return ModelGateStoreResult(
            correlation_id=request.correlation_id,
            operation=request.operation,
            outcome=outcome,
            epoch=meta.epoch,
            counted=self._counted(cur, piece) if piece is not None else 0,
            holder=meta.holder,
            order_position=meta.order_position,
            detail=detail,
        )

    def _write(
        self,
        cur: Any,
        meta: _Meta,
        request: ModelGateStoreRequest,
        piece: str | None = None,
    ) -> ModelGateStoreResult:
        meta.epoch += 1
        meta.dirty = True
        return self._answer(cur, meta, request, EnumGateStoreOutcome.OK, piece=piece)

    def _apply(self, cur: Any, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        op = request.operation
        lock = "" if op is EnumGateStoreOperation.READ else " FOR UPDATE"
        cur.execute("SELECT epoch, holder, order_position FROM gate_store_meta" + lock)
        epoch, holder, position = cur.fetchone()
        meta = _Meta(epoch=int(epoch), holder=holder, order_position=int(position))
        if op is EnumGateStoreOperation.READ:
            return self._answer(
                cur, meta, request, EnumGateStoreOutcome.OK, piece=request.piece
            )
        if request.step_key is None:
            return self._answer(
                cur,
                meta,
                request,
                EnumGateStoreOutcome.REFUSED_INVALID,
                "a mutating request carries a step key",
            )
        cur.execute(
            "SELECT result FROM gate_store_step WHERE step_key = %s",
            (request.step_key,),
        )
        row = cur.fetchone()
        if row is not None:
            first = ModelGateStoreResult.model_validate(row[0])
            return first.model_copy(
                update={
                    "correlation_id": request.correlation_id,
                    "outcome": EnumGateStoreOutcome.DUPLICATE,
                    "detail": f"replay of {request.step_key}: first answer {first.outcome}",
                }
            )
        handlers = {
            EnumGateStoreOperation.RESERVE: self._reserve,
            EnumGateStoreOperation.CONSUME: self._consume,
            EnumGateStoreOperation.RELEASE: self._release,
            EnumGateStoreOperation.RECORD_SAFETY: self._record_safety,
            EnumGateStoreOperation.HANDOVER: self._handover,
            EnumGateStoreOperation.ADVANCE_ORDER: self._advance_order,
        }
        result = handlers[op](cur, meta, request)
        if meta.dirty:
            cur.execute(
                "UPDATE gate_store_meta SET epoch = %s, holder = %s, order_position = %s",
                (meta.epoch, meta.holder, meta.order_position),
            )
        cur.execute(
            "INSERT INTO gate_store_step (step_key, result) VALUES (%s, %s::jsonb)",
            (request.step_key, result.model_dump_json()),
        )
        return result

    @staticmethod
    def _sweep(cur: Any, now: float) -> None:
        cur.execute(
            "DELETE FROM gate_store_reservation WHERE NOT consumed AND expires_at <= %s",
            (now,),
        )

    def _reserve(
        self, cur: Any, meta: _Meta, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        piece, rid = request.piece, request.reservation_id
        if piece is None or rid is None:
            return self._answer(
                cur, meta, request, EnumGateStoreOutcome.REFUSED_INVALID, "piece and id"
            )
        limit = self._policy.limits.get(piece)
        if limit is None:
            return self._answer(
                cur, meta, request, EnumGateStoreOutcome.REFUSED_UNKNOWN_PIECE, piece
            )
        cur.execute("SELECT state FROM gate_store_safety WHERE piece = %s", (piece,))
        safety = cur.fetchone()
        if safety is not None and safety[0] == "stop":
            return self._answer(
                cur, meta, request, EnumGateStoreOutcome.REFUSED_STOP, piece, piece
            )
        self._sweep(cur, request.now)
        cur.execute(
            "SELECT 1 FROM gate_store_reservation WHERE reservation_id = %s", (rid,)
        )
        if cur.fetchone() is not None:
            return self._answer(
                cur,
                meta,
                request,
                EnumGateStoreOutcome.REFUSED_INVALID,
                "reservation id in use",
                piece,
            )
        if self._counted(cur, piece) >= limit:
            return self._answer(
                cur, meta, request, EnumGateStoreOutcome.REFUSED_LIMIT, piece, piece
            )
        cur.execute(
            "INSERT INTO gate_store_reservation (reservation_id, piece, expires_at)"
            " VALUES (%s, %s, %s)",
            (rid, piece, request.now + self._policy.reservation_ttl_seconds),
        )
        return self._write(cur, meta, request, piece)

    def _consume(
        self, cur: Any, meta: _Meta, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        cur.execute(
            "SELECT piece, expires_at, consumed FROM gate_store_reservation"
            " WHERE reservation_id = %s",
            (request.reservation_id or "",),
        )
        row = cur.fetchone()
        if row is None:
            return self._answer(
                cur, meta, request, EnumGateStoreOutcome.REFUSED_UNKNOWN_RESERVATION
            )
        piece, expires_at, consumed = row
        if consumed:
            return self._answer(
                cur, meta, request, EnumGateStoreOutcome.REFUSED_CONSUMED, piece=piece
            )
        if expires_at <= request.now:
            self._sweep(cur, request.now)
            return self._answer(
                cur, meta, request, EnumGateStoreOutcome.REFUSED_EXPIRED, piece=piece
            )
        cur.execute("SELECT state FROM gate_store_safety WHERE piece = %s", (piece,))
        safety = cur.fetchone()
        if safety is not None and safety[0] == "stop":
            return self._answer(
                cur, meta, request, EnumGateStoreOutcome.REFUSED_STOP, piece=piece
            )
        if request.expected_epoch is not None and request.expected_epoch != meta.epoch:
            return self._answer(
                cur,
                meta,
                request,
                EnumGateStoreOutcome.REFUSED_FENCE,
                "the epoch advanced since the reservation was decided",
                piece,
            )
        cur.execute(
            "UPDATE gate_store_reservation SET consumed = true WHERE reservation_id = %s",
            (request.reservation_id,),
        )
        return self._write(cur, meta, request, piece)

    def _release(
        self, cur: Any, meta: _Meta, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        rid = request.reservation_id or ""
        cur.execute(
            "SELECT piece, consumed FROM gate_store_reservation WHERE reservation_id = %s",
            (rid,),
        )
        row = cur.fetchone()
        if row is None:
            return self._answer(
                cur, meta, request, EnumGateStoreOutcome.REFUSED_UNKNOWN_RESERVATION
            )
        piece, consumed = row
        if consumed and not request.process_gone:
            return self._answer(
                cur,
                meta,
                request,
                EnumGateStoreOutcome.REFUSED_PROCESS_LIVE,
                "a consumed reservation counts until its process is read as gone",
                piece,
            )
        cur.execute(
            "DELETE FROM gate_store_reservation WHERE reservation_id = %s", (rid,)
        )
        return self._write(cur, meta, request, piece)

    def _record_safety(
        self, cur: Any, meta: _Meta, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        piece = request.piece
        if (
            piece is None
            or piece not in self._policy.limits
            or request.safety_state not in _SAFETY_STATES
        ):
            return self._answer(
                cur,
                meta,
                request,
                EnumGateStoreOutcome.REFUSED_INVALID,
                "a known piece and a state of stop or clear",
            )
        cur.execute(
            "INSERT INTO gate_store_safety (piece, state) VALUES (%s, %s)"
            " ON CONFLICT (piece) DO UPDATE SET state = EXCLUDED.state",
            (piece, request.safety_state),
        )
        return self._write(cur, meta, request, piece)

    def _handover(
        self, cur: Any, meta: _Meta, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        if request.holder is None or request.expected_epoch is None:
            return self._answer(
                cur,
                meta,
                request,
                EnumGateStoreOutcome.REFUSED_INVALID,
                "holder and epoch",
            )
        if request.expected_epoch != meta.epoch:
            return self._answer(
                cur, meta, request, EnumGateStoreOutcome.REFUSED_FENCE, "stale epoch"
            )
        meta.holder = request.holder
        return self._write(cur, meta, request)

    def _advance_order(
        self, cur: Any, meta: _Meta, request: ModelGateStoreRequest
    ) -> ModelGateStoreResult:
        if (
            request.holder is None
            or request.expected_epoch is None
            or request.order_position is None
        ):
            return self._answer(
                cur,
                meta,
                request,
                EnumGateStoreOutcome.REFUSED_INVALID,
                "holder, epoch and position",
            )
        if request.holder != meta.holder or request.expected_epoch != meta.epoch:
            return self._answer(
                cur,
                meta,
                request,
                EnumGateStoreOutcome.REFUSED_FENCE,
                "not the holder at this epoch",
            )
        if (
            request.order_position != meta.order_position + 1
            or request.order_position > len(self._policy.order)
        ):
            return self._answer(
                cur,
                meta,
                request,
                EnumGateStoreOutcome.REFUSED_ORDER,
                "the order advances one position at a time within its length",
            )
        meta.order_position = request.order_position
        return self._write(cur, meta, request)
