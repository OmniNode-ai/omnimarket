# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Postgres backend of the gate store (plan section 7.5: ``timer_store``).

One request is one database transaction. The transaction locks the single ``gate_store_state``
row first (``SELECT ... FOR UPDATE``), so requests are decided one at a time on the committed
state: nine racing reservations stay within the limit, and a request killed at any stage rolls
back and leaves no trace. The rules are the in-memory twin's own (``InMemoryGateStore._apply``)
run on the state the transaction loaded, so the two backends cannot disagree on a rule; the
frozen contract suite proves it for each. The relations come only from the node's migrations.
The connection is injected: the overlay names the DSN variable, the runtime builds the connect
function, and a test passes its own.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from psycopg2 import sql

from omnimarket.models.model_gate_store import (
    ModelGateStorePolicy,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
    _Reservation,
    _State,
)

Connect = Callable[[], Any]


class PostgresGateStore(InMemoryGateStore):
    """The serializable store on Postgres; the rules are the twin's, the state is the database's."""

    #: The lock every request takes on the state row first; a test break empties it.
    _LOCK_CLAUSE = "FOR UPDATE"

    def __init__(
        self,
        policy: ModelGateStorePolicy,
        connect: Connect,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(policy, fault_hook)
        if policy.postgres is None:
            raise ValueError("the postgres backend needs the overlay's postgres block")
        self._connect = connect
        self._schema = policy.postgres.schema_name
        self._lock_timeout_ms = policy.postgres.lock_timeout_ms

    def _t(self, name: str) -> sql.Composed:
        return sql.SQL("{}.{}").format(
            sql.Identifier(self._schema), sql.Identifier(name)
        )

    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        """One request in one transaction; any exception rolls the whole of it back."""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SET LOCAL lock_timeout = {}").format(
                        sql.Literal(f"{self._lock_timeout_ms}ms")
                    )
                )
                state = self._load(cur, request)
                self._at("before-apply")
                before = (state.epoch, state.holder, state.order_position)
                held = {
                    k: (r.piece, r.expires_at, r.consumed)
                    for k, r in state.reservations.items()
                }
                safety = dict(state.safety)
                result = self._apply(state, request)
                self._store(cur, state, request, before, held, safety)
                self._at("after-apply")
                self._at("before-commit")
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _load(self, cur: Any, request: ModelGateStoreRequest) -> _State:
        cur.execute(
            sql.SQL(
                "SELECT epoch, holder, order_position FROM {} WHERE singleton {}"
            ).format(self._t("gate_store_state"), sql.SQL(self._LOCK_CLAUSE))
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("gate_store_state has no row: the migration did not run")
        state = _State(epoch=int(row[0]), holder=row[1], order_position=int(row[2]))
        cur.execute(
            sql.SQL(
                "SELECT reservation_id, piece, expires_at, consumed FROM {}"
            ).format(self._t("gate_store_reservations"))
        )
        for rid, piece, expires_at, consumed in cur.fetchall():
            state.reservations[rid] = _Reservation(
                piece, float(expires_at), bool(consumed)
            )
        cur.execute(
            sql.SQL("SELECT piece, safety_state FROM {}").format(
                self._t("gate_store_safety")
            )
        )
        state.safety = dict(cur.fetchall())
        if request.step_key is not None:
            cur.execute(
                sql.SQL("SELECT result FROM {} WHERE step_key = %s").format(
                    self._t("gate_store_steps")
                ),
                (request.step_key,),
            )
            first = cur.fetchone()
            if first is not None:
                stored = (
                    first[0] if isinstance(first[0], dict) else json.loads(first[0])
                )
                state.steps[request.step_key] = ModelGateStoreResult.model_validate(
                    stored
                )
        return state

    def _store(
        self,
        cur: Any,
        state: _State,
        request: ModelGateStoreRequest,
        before: tuple[int, str | None, int],
        held: dict[str, tuple[str, float, bool]],
        safety: dict[str, str],
    ) -> None:
        if (state.epoch, state.holder, state.order_position) != before:
            cur.execute(
                sql.SQL(
                    "UPDATE {} SET epoch = %s, holder = %s, order_position = %s WHERE singleton"
                ).format(self._t("gate_store_state")),
                (state.epoch, state.holder, state.order_position),
            )
        now = {
            k: (r.piece, r.expires_at, r.consumed)
            for k, r in state.reservations.items()
        }
        for rid in held.keys() - now.keys():
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE reservation_id = %s").format(
                    self._t("gate_store_reservations")
                ),
                (rid,),
            )
        for rid, values in now.items():
            if held.get(rid) != values:
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {} (reservation_id, piece, expires_at, consumed) "
                        "VALUES (%s, %s, %s, %s) ON CONFLICT (reservation_id) "
                        "DO UPDATE SET piece = EXCLUDED.piece, expires_at = EXCLUDED.expires_at, "
                        "consumed = EXCLUDED.consumed"
                    ).format(self._t("gate_store_reservations")),
                    (rid, *values),
                )
        for piece, value in state.safety.items():
            if safety.get(piece) != value:
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {} (piece, safety_state) VALUES (%s, %s) "
                        "ON CONFLICT (piece) DO UPDATE SET safety_state = EXCLUDED.safety_state"
                    ).format(self._t("gate_store_safety")),
                    (piece, value),
                )
        key = request.step_key
        if key is not None and key in state.steps:
            cur.execute(
                sql.SQL(
                    "INSERT INTO {} (step_key, result) VALUES (%s, %s::jsonb) "
                    "ON CONFLICT (step_key) DO NOTHING"
                ).format(self._t("gate_store_steps")),
                (key, json.dumps(state.steps[key].model_dump(mode="json"))),
            )
