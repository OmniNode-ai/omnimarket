# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only Postgres adapter for the ``tenant_inference_credentials`` projection.

Never writes: the projection is written only by its consumer node. Tenant
isolation is the bound ``tenant_id`` predicate; the ``app.tenant_id`` setting is
applied for the query's transaction as defence in depth. The connection string is
a secret read by logical name at query time, never from the process environment.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable, Mapping
from typing import Any

from omnimarket.handlers.inference_credentials_http.errors import (
    CredentialReadModelMissingTableError,
    CredentialReadModelNotConfiguredError,
    CredentialReadModelUnavailableError,
)
from omnimarket.protocols.protocol_inference_credential_http import (
    ProtocolSecretValueSource,
)

ANALYTICS_DSN_SECRET_NAME = "inference.credentials.analytics_dsn"
_LIST_SQL = (
    "SELECT api_key_ref, name, provider, created_at, revoked_at "
    "FROM tenant_inference_credentials "
    "WHERE tenant_id = %(tenant)s ORDER BY created_at DESC"
)
_OWNED_SQL = (
    "SELECT api_key_ref, name, provider, created_at, revoked_at "
    "FROM tenant_inference_credentials "
    "WHERE api_key_ref = %(ref)s AND tenant_id = %(tenant)s"
)

Connect = Callable[[str], Any]


def _psycopg2_connect(dsn: str) -> Any:
    driver = importlib.import_module("psycopg2")
    return driver.connect(dsn, connect_timeout=5)


class PostgresInferenceCredentialReadModel:
    """Query the projection through a DSN resolved by logical name."""

    def __init__(
        self,
        secret_source: ProtocolSecretValueSource,
        *,
        connect: Connect = _psycopg2_connect,
    ) -> None:
        self._secrets = secret_source
        self._connect = connect

    def _query(
        self, dsn: str, sql: str, params: dict[str, str], tenant_id: str
    ) -> list[dict[str, Any]]:
        driver = importlib.import_module("psycopg2")
        errors = importlib.import_module("psycopg2.errors")
        extras = importlib.import_module("psycopg2.extras")

        try:
            connection = self._connect(dsn)
            try:
                with (
                    connection,
                    connection.cursor(cursor_factory=extras.RealDictCursor) as cursor,
                ):
                    cursor.execute(
                        "SELECT set_config('app.tenant_id', %s, true)", (tenant_id,)
                    )
                    cursor.execute(sql, params)
                    return [dict(row) for row in cursor.fetchall()]
            finally:
                connection.close()
        except errors.UndefinedTable as exc:
            raise CredentialReadModelMissingTableError(
                "tenant_inference_credentials is absent"
            ) from exc
        except driver.Error as exc:
            # The message stays coarse: a connection error embeds the DSN.
            raise CredentialReadModelUnavailableError(
                f"credential projection error ({type(exc).__name__})"
            ) from None

    async def _run(
        self, sql: str, params: dict[str, str], tenant_id: str
    ) -> list[Mapping[str, Any]]:
        dsn = (await self._secrets.get(ANALYTICS_DSN_SECRET_NAME) or "").strip()
        if not dsn:
            raise CredentialReadModelNotConfiguredError(
                "the credential projection connection is not mapped"
            )
        rows = await asyncio.to_thread(self._query, dsn, sql, params, tenant_id)
        return list(rows)

    async def list_for_tenant(self, tenant_id: str) -> list[Mapping[str, Any]]:
        return await self._run(_LIST_SQL, {"tenant": tenant_id}, tenant_id)

    async def owned_by_tenant(
        self, api_key_ref: str, tenant_id: str
    ) -> Mapping[str, Any] | None:
        rows = await self._run(
            _OWNED_SQL, {"ref": api_key_ref, "tenant": tenant_id}, tenant_id
        )
        return rows[0] if rows else None
