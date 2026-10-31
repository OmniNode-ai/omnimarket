# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant credential projection's writes on the two sync stores.

``CredentialSqliteStore`` and ``CredentialPgStore`` hold the credential and
route writes, the local schema and the one-time local store steps of this
node's two tables (OMN-19968, OMN-21217). ``SqliteDatabaseAdapter`` and
``PostgresSyncProjectionAdapter`` inherit them, so every method keeps its
name and behaviour on the adapter.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from omnimarket.models.model_credential_route_write import ModelCredentialRouteWrite
from omnimarket.models.model_credential_row_write import ModelCredentialRowWrite
from omnimarket.protocols.protocol_credential_last_used_write import (
    credential_used_at_instant,
)

# OMN-19968: the local half of the tenant BYOK credential projection's two
# tables, declared from node_projection_tenant_credentials/migrations (0000,
# 0001: name and provider nullable for a revoke tombstone) and
# node_delegation_routing_reducer/migrations (0001, 0004: provider). Timestamps
# are ISO text; the primary key and the (tenant_id, task_type) unique key back
# the two upserts in handler_tenant_credentials_store.
_TENANT_INFERENCE_CREDENTIALS_DDL = """
CREATE TABLE IF NOT EXISTS tenant_inference_credentials (
    api_key_ref TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    name        TEXT,
    provider    TEXT,
    created_at  TEXT NOT NULL,
    revoked_at  TEXT,
    fingerprint TEXT,
    set_at      TEXT,
    expires_at  TEXT,
    bound_host  TEXT,
    workspace_id TEXT,
    models      TEXT,
    version     INTEGER NOT NULL DEFAULT 1,
    last_used_at TEXT
)
"""


# 0005: a file made before the fingerprint and set time existed gains them on open,
# so a read of the exposure's declared columns never meets a missing one.
_TENANT_INFERENCE_CREDENTIALS_ADDED_COLUMNS: dict[str, object] = {
    "fingerprint": None,
    "set_at": None,
    # 0006 (OMN-20991): the key's expiry date.
    "expires_at": None,
    # 0007 (OMN-21012): the lab host the key is bound to.
    "bound_host": None,
    # 0008 (OMN-20967): the Anthropic workspace the key belongs to.
    "workspace_id": None,
    "models": None,
    # 0009 (OMN-21217): the row's version, published with it on the compacted
    # credential-state topic. The local store never replays that topic, but it
    # raises the version on each write exactly as the Postgres runner does, so
    # both stores hold the same row (tests/test_omn19968_writer_store_parity.py).
    "version": 1,
    # 0010 (OMN-19996): the terminal time of the latest run that used the key,
    # ISO text; NULL until a run names it.
    "last_used_at": None,
}


# OMN-21217: the columns _ensure_columns adds carry no default, so a store made
# before them holds NULL there. One recorded step fills them, so a store opens
# with reads only once it has run, and a read-only store is read as it is.
_CREDENTIAL_ROUTE_VERSIONS_STEP = "omn21217_credential_route_versions"


_CREDENTIAL_CHOSEN_MODELS_STEP = "omn21217_credential_chosen_models"


_DELEGATION_ROUTING_TENANT_OVERLAY_DDL = """
CREATE TABLE IF NOT EXISTS delegation_routing_tenant_overlay (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL,
    task_type    TEXT NOT NULL,
    backend_id   TEXT NOT NULL,
    provider     TEXT,
    endpoint_url TEXT NOT NULL,
    model_name   TEXT NOT NULL,
    models       TEXT NOT NULL DEFAULT '[]',
    version      INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    secret_ref   TEXT,
    timeout_ms   INTEGER CHECK (timeout_ms IS NULL OR timeout_ms > 0),
    max_tokens   INTEGER CHECK (max_tokens IS NULL OR max_tokens > 0),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (tenant_id, task_type)
)
"""


if TYPE_CHECKING:

    class _SqliteHost(Protocol):
        """What the SQLite mixin calls on ``SqliteDatabaseAdapter``."""

        def _connect(self) -> sqlite3.Connection: ...

        @staticmethod
        def _encode(column: str, value: object) -> object: ...

        @staticmethod
        def _store_step_recorded(conn: sqlite3.Connection, step: str) -> bool: ...

        def _ensure_columns(
            self, conn: sqlite3.Connection, table: str, row: dict[str, object]
        ) -> None: ...

    class _PgHost(Protocol):
        """What the Postgres mixin calls on ``PostgresSyncProjectionAdapter``."""

        def _connect(self) -> Any: ...

        @staticmethod
        def _tenant_scoped(conn: Any, tenant: str) -> AbstractContextManager[None]: ...

        @staticmethod
        def _adapt(value: object) -> object: ...

    _SqliteBase = _SqliteHost
    _PgBase = _PgHost
else:
    _SqliteBase = object
    _PgBase = object


class CredentialSqliteStore(_SqliteBase):
    """The credential writes and store steps of ``SqliteDatabaseAdapter``."""

    def _ensure_credential_route_columns(self, conn: sqlite3.Connection) -> None:
        self._ensure_columns(
            conn,
            "delegation_routing_tenant_overlay",
            {"models": None, "version": 1},
        )
        conn.commit()

    @classmethod
    def _credential_store_steps(
        cls, conn: sqlite3.Connection
    ) -> list[tuple[str, Callable[[sqlite3.Connection], None], str]]:
        pending: list[tuple[str, Callable[[sqlite3.Connection], None], str]] = []
        if not cls._store_step_recorded(conn, _CREDENTIAL_ROUTE_VERSIONS_STEP):
            pending.append(
                (
                    _CREDENTIAL_ROUTE_VERSIONS_STEP,
                    cls._fill_credential_route_versions,
                    "gave every credential and route a version and every route its model list",
                )
            )
        if not cls._store_step_recorded(conn, _CREDENTIAL_CHOSEN_MODELS_STEP):
            pending.append(
                (
                    _CREDENTIAL_CHOSEN_MODELS_STEP,
                    cls._backfill_credential_chosen_models,
                    "recovered only unambiguous choices on their owning credential ref",
                )
            )
        return pending

    @staticmethod
    def _decoded_credential_models(
        table: str, result: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if table in {
            "delegation_routing_tenant_overlay",
            "tenant_inference_credentials",
        }:
            for row in result:
                if isinstance(row.get("models"), str):
                    try:
                        row["models"] = json.loads(row["models"])
                    except json.JSONDecodeError:
                        # Preserve malformed catalogue data as invalid input
                        # for the resolver; one key must not disable the pool.
                        if table == "delegation_routing_tenant_overlay":
                            raise
        return result

    @staticmethod
    def _fill_credential_route_versions(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE delegation_routing_tenant_overlay SET version = 1 WHERE version IS NULL"
        )
        conn.execute(
            "UPDATE tenant_inference_credentials SET version = 1 WHERE version IS NULL"
        )
        conn.execute(
            "UPDATE delegation_routing_tenant_overlay SET models = CASE "
            "WHEN model_name IS NULL OR trim(model_name) = '' "
            "OR model_name = 'byok-model-unresolved' THEN '[]' "
            "ELSE json_array(model_name) END WHERE models IS NULL"
        )

    @staticmethod
    def _backfill_credential_chosen_models(conn: sqlite3.Connection) -> None:
        """One-time local migration, never a model fallback at request time.

        Another pooled key cannot inherit an overlay's choices. Multiple
        overlays naming the same ref must all record the same valid list;
        otherwise its nullable catalogue choice remains unknown.
        """
        rows = conn.execute(
            "SELECT c.tenant_id, c.api_key_ref, c.provider, o.models "
            "FROM tenant_inference_credentials c "
            "JOIN delegation_routing_tenant_overlay o ON o.tenant_id = c.tenant_id "
            "AND o.secret_ref = c.api_key_ref AND o.provider = c.provider "
            "WHERE c.models IS NULL"
        ).fetchall()
        choices_by_ref: dict[tuple[str, str, str], set[tuple[str, ...] | None]] = {}
        for row in rows:
            key = (str(row["tenant_id"]), str(row["api_key_ref"]), str(row["provider"]))
            try:
                parsed: object = json.loads(row["models"])
            except (TypeError, json.JSONDecodeError):
                parsed = None
            chosen: tuple[str, ...] | None = None
            if (
                isinstance(parsed, list)
                and parsed
                and all(
                    isinstance(model, str) and model.strip() and len(model) <= 200
                    for model in parsed
                )
                and len(set(parsed)) == len(parsed)
            ):
                chosen = tuple(str(model) for model in parsed)
            choices_by_ref.setdefault(key, set()).add(chosen)
        for (_tenant, ref, provider), choices in choices_by_ref.items():
            if len(choices) != 1 or None in choices:
                continue
            chosen = next(iter(choices))
            assert chosen is not None
            # api_key_ref is the key; the tenant was matched in the join.
            conn.execute(
                "UPDATE tenant_inference_credentials SET models = ? "
                "WHERE api_key_ref = ? AND provider = ? AND models IS NULL",
                (json.dumps(chosen), ref, provider),
            )

    def write_credential_row(self, plan: ModelCredentialRowWrite) -> int | None:
        """OMN-21217: one statement; the version is raised in SQL, never read first."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            parameters = {
                key: self._encode(key, value) for key, value in plan.parameters.items()
            }
            # The runner's NOW(), as upsert_returning supplies it on this store.
            parameters["now"] = datetime.now(tz=UTC).isoformat()
            written = conn.execute(plan.sqlite_write, parameters).fetchone()
            conn.commit()
            return None if written is None else int(written[0])
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def replace_credential_route(
        self,
        plan: ModelCredentialRouteWrite,
    ) -> tuple[bool, int | None]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            written = conn.execute(
                plan.sqlite_write,
                {
                    key: self._encode(key, value)
                    for key, value in plan.parameters.items()
                },
            ).fetchone()
            if written is not None and plan.sqlite_models_write is not None:
                models_written = conn.execute(
                    plan.sqlite_models_write,
                    {
                        key: self._encode(key, value)
                        for key, value in plan.parameters.items()
                    },
                ).fetchone()
                if models_written is None:
                    raise RuntimeError("credential model target is unavailable")
            current = conn.execute(
                plan.sqlite_read,
                {
                    key: self._encode(key, value)
                    for key, value in plan.parameters.items()
                },
            ).fetchone()
            conn.commit()
            return written is not None, None if current is None else int(current[0])
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_credential_last_used(
        self, api_key_ref: str, used_at: str, *, tenant_id: str | None = None
    ) -> bool:
        """OMN-19996: move the key's last-used time forward, never insert.

        SQLite has no time type, so the stored text cannot be compared in SQL:
        ``05:00+02:00`` sorts after ``04:00+00:00`` though it is earlier. The
        stored time is read and compared as an instant inside one immediate
        transaction, so a row written in any ISO form (including one stored
        before this rule) is never lowered, and no second writer interleaves.
        A stored value that is not a time is replaced. ``tenant_id`` adds a
        tenant predicate; ``None`` adds none (the local store).
        """
        new_instant = credential_used_at_instant(used_at)
        where = "api_key_ref = :ref"
        params = {"ref": api_key_ref, "used_at": used_at}
        if tenant_id is not None:
            where += " AND tenant_id = :tenant"
            params["tenant"] = tenant_id
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT last_used_at FROM tenant_inference_credentials WHERE {where}",
                params,
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            stored = row["last_used_at"]
            if stored is not None:
                try:
                    stored_instant: datetime | None = credential_used_at_instant(
                        str(stored)
                    )
                except ValueError:
                    stored_instant = None
                if stored_instant is not None and stored_instant >= new_instant:
                    conn.rollback()
                    return False
            conn.execute(
                "UPDATE tenant_inference_credentials SET last_used_at = :used_at "
                f"WHERE {where}",
                params,
            )
            conn.commit()
            return True
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


class CredentialPgStore(_PgBase):
    """The credential writes of ``PostgresSyncProjectionAdapter``."""

    def write_credential_row(self, plan: ModelCredentialRowWrite) -> int | None:
        """OMN-21217: one statement; the version is raised in SQL, never read first."""
        conn = self._connect()
        try:
            with self._tenant_scoped(conn, plan.tenant_id), conn.cursor() as cur:
                cur.execute(
                    plan.postgres_write,
                    {key: self._adapt(value) for key, value in plan.parameters.items()},
                )
                written = cur.fetchone()
                return None if written is None else int(written[0])
        finally:
            conn.close()

    def replace_credential_route(
        self,
        plan: ModelCredentialRouteWrite,
    ) -> tuple[bool, int | None]:
        conn = self._connect()
        try:
            with (
                self._tenant_scoped(conn, str(plan.row["tenant_id"])),
                conn.cursor() as cur,
            ):
                cur.execute(
                    plan.postgres_write,
                    {key: self._adapt(value) for key, value in plan.parameters.items()},
                )
                written = cur.fetchone()
                if written is not None and plan.postgres_models_write is not None:
                    # OMN-21217: the key's own choices change with its route or
                    # not at all; raising rolls both back.
                    cur.execute(
                        plan.postgres_models_write,
                        {
                            key: self._adapt(value)
                            for key, value in plan.parameters.items()
                        },
                    )
                    if cur.fetchone() is None:
                        raise RuntimeError("credential model target is unavailable")
                cur.execute(
                    plan.postgres_read,
                    {key: self._adapt(value) for key, value in plan.parameters.items()},
                )
                current = cur.fetchone()
                return written is not None, None if current is None else int(current[0])
        finally:
            conn.close()

    def record_credential_last_used(
        self, api_key_ref: str, used_at: str, *, tenant_id: str | None = None
    ) -> bool:
        """OMN-19996: move the key's last-used time forward, never insert.

        TIMESTAMPTZ compares instants, so one guarded UPDATE suffices. With a
        ``tenant_id`` it runs under that tenant like every tenant-scoped write
        here and names it too, so another tenant's key with the same ref is
        never touched whether or not the table carries a row-level policy.
        ``None`` adds no tenant predicate and no tenant scope.
        """
        statement = (
            "UPDATE tenant_inference_credentials "
            "SET last_used_at = %(used_at)s::TIMESTAMPTZ "
            "WHERE api_key_ref = %(ref)s "
            "AND (last_used_at IS NULL OR last_used_at < %(used_at)s::TIMESTAMPTZ)"
        )
        params: dict[str, object] = {"ref": api_key_ref, "used_at": used_at}
        conn = self._connect()
        try:
            if tenant_id is None:
                with conn.cursor() as cur:
                    cur.execute(statement, params)
                    return bool(cur.rowcount > 0)
            with self._tenant_scoped(conn, tenant_id), conn.cursor() as cur:
                cur.execute(
                    statement + " AND tenant_id = %(tenant)s",
                    {**params, "tenant": tenant_id},
                )
                return bool(cur.rowcount > 0)
        finally:
            conn.close()
