# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Atomic whole-list credential route replacement at the database boundary."""

from __future__ import annotations

from collections.abc import Callable
from types import MappingProxyType

from omnibase_core.models.projection.model_upsert_plan import build_upsert_plan

from omnimarket.models.model_credential_route_write import ModelCredentialRouteWrite
from omnimarket.models.model_credential_row_write import ModelCredentialRowWrite

_CREDENTIALS = "tenant_inference_credentials"


class HandlerCredentialRouteWrite:
    """Bind one guarded SQL statement for the existing projection writer."""

    @staticmethod
    def prepare(
        row: dict[str, object], expected_version: int | None
    ) -> ModelCredentialRouteWrite:
        """Own the complete route SQL; adapters only execute this bound plan."""
        sqlite_write, parameters = HandlerCredentialRouteWrite.statement(
            row, expected_version, postgres=False
        )
        postgres_write, _ = HandlerCredentialRouteWrite.statement(
            row, expected_version, postgres=True
        )
        return ModelCredentialRouteWrite(
            row=MappingProxyType(dict(row)),
            expected_version=expected_version,
            parameters=MappingProxyType(parameters),
            sqlite_write=sqlite_write,
            postgres_write=postgres_write,
            sqlite_read=(
                "SELECT version FROM delegation_routing_tenant_overlay "
                "WHERE tenant_id = :tenant_id AND task_type = :task_type"
            ),
            postgres_read=(
                "SELECT version FROM delegation_routing_tenant_overlay "
                "WHERE tenant_id = %(tenant_id)s AND task_type = %(task_type)s"
            ),
            sqlite_models_write=(
                # An empty choice is an unknown one: NULL, as a revoke-first
                # delivery order leaves it. The version rises as the runner's
                # does (OMN-21217), so both stores hold the same row.
                "UPDATE tenant_inference_credentials SET models = NULLIF(:models, '[]'), "
                "version = version + 1 "
                "WHERE tenant_id = :tenant_id AND api_key_ref = :secret_ref "
                "AND revoked_at IS NULL RETURNING api_key_ref"
                if row.get("secret_ref") is not None
                else None
            ),
            # The same choice on Postgres, in the route write's transaction.
            # The version rises as the asynchronous writer's does, so a state
            # row read back afterwards never undoes this write.
            postgres_models_write=(
                "UPDATE tenant_inference_credentials "
                "SET models = NULLIF(%(models)s::JSONB, '[]'::JSONB), "
                "version = version + 1 "
                "WHERE tenant_id = %(tenant_id)s AND api_key_ref = %(secret_ref)s "
                "AND revoked_at IS NULL RETURNING api_key_ref"
                if row.get("secret_ref") is not None
                else None
            ),
        )

    @staticmethod
    def statement(
        row: dict[str, object],
        expected_version: int | None,
        *,
        postgres: bool,
    ) -> tuple[str, dict[str, object]]:
        """One guarded statement; expected-version writes never insert missing rows.

        Identifier validation is delegated to the canonical upsert-plan builder.
        The credential tombstone guard also applies to the conflict-update arm.
        """
        table = "delegation_routing_tenant_overlay"
        build_upsert_plan(table=table, conflict_key="tenant_id,task_type", row=row)
        bind: Callable[[str], str] = (
            (lambda key: f"%({key})s") if postgres else (lambda key: f":{key}")
        )
        params = dict(row)
        guard = (
            "NOT EXISTS (SELECT 1 FROM tenant_inference_credentials "
            f"WHERE api_key_ref = {bind('secret_ref')} AND revoked_at IS NOT NULL)"
        )
        fields = [
            key for key in row if key not in {"tenant_id", "task_type", "created_at"}
        ]
        if expected_version is not None:
            params["expected_version"] = expected_version
            updates = ", ".join(f"{key} = {bind(key)}" for key in fields)
            statement = (
                f"UPDATE {table} SET {updates}, version = version + 1 "
                f"WHERE tenant_id = {bind('tenant_id')} AND task_type = {bind('task_type')} "
                f"AND version = {bind('expected_version')} AND {guard} RETURNING version"
            )
        else:
            keys = list(row)
            updates = ", ".join(f"{key} = EXCLUDED.{key}" for key in fields)
            statement = (
                f"INSERT INTO {table} ({', '.join(keys)}, version) "
                f"SELECT {', '.join(bind(key) for key in keys)}, 1 WHERE {guard} "
                f"ON CONFLICT (tenant_id, task_type) DO UPDATE SET {updates}, "
                f"version = {table}.version + 1 WHERE {guard} RETURNING version"
            )
        return statement, params


class HandlerCredentialRowWrite:
    """The credential catalog row's register and revoke as one atomic statement.

    OMN-21217: the same writes the asynchronous runner makes
    (``handler_tenant_credentials_projection``), with the version raised in
    SQL on conflict, so the sync stores hold the runner's versions without a
    read-then-write that two writers could interleave.
    """

    @staticmethod
    def registered(row: dict[str, object]) -> ModelCredentialRowWrite:
        """Insert, or overwrite the named columns and raise the version.

        ``row`` names only the columns the event carries; an absent optional
        column is left as stored, as the runner's COALESCE leaves it.
        """
        build_upsert_plan(table=_CREDENTIALS, conflict_key="api_key_ref", row=row)
        columns = list(row)
        updates = ", ".join(
            f"{column} = EXCLUDED.{column}"
            for column in columns
            if column != "api_key_ref"
        )

        def statement(bind: Callable[[str], str], now: str) -> str:
            return (
                f"INSERT INTO {_CREDENTIALS} ({', '.join(columns)}, created_at) "
                f"VALUES ({', '.join(bind(column) for column in columns)}, {now}) "
                f"ON CONFLICT (api_key_ref) DO UPDATE SET {updates}, "
                f"version = {_CREDENTIALS}.version + 1 RETURNING version"
            )

        return ModelCredentialRowWrite(
            tenant_id=str(row["tenant_id"]),
            parameters=MappingProxyType(dict(row)),
            sqlite_write=statement(lambda key: f":{key}", ":now"),
            postgres_write=statement(lambda key: f"%({key})s", "NOW()"),
        )

    @staticmethod
    def revoked(api_key_ref: str, tenant_id: str) -> ModelCredentialRowWrite:
        """Tombstone an unknown key, else keep the earliest revoked_at; raise the version."""

        def statement(bind: Callable[[str], str], now: str) -> str:
            return (
                f"INSERT INTO {_CREDENTIALS} "
                "(api_key_ref, tenant_id, name, provider, created_at, revoked_at) "
                f"VALUES ({bind('api_key_ref')}, {bind('tenant_id')}, NULL, NULL, "
                f"{now}, {now}) ON CONFLICT (api_key_ref) DO UPDATE SET "
                f"revoked_at = COALESCE({_CREDENTIALS}.revoked_at, EXCLUDED.revoked_at), "
                f"version = {_CREDENTIALS}.version + 1 RETURNING version"
            )

        return ModelCredentialRowWrite(
            tenant_id=tenant_id,
            parameters=MappingProxyType(
                {"api_key_ref": api_key_ref, "tenant_id": tenant_id}
            ),
            sqlite_write=statement(lambda key: f":{key}", ":now"),
            postgres_write=statement(lambda key: f"%({key})s", "NOW()"),
        )
