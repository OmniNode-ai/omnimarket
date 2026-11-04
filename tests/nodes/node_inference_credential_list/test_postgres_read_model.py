# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection reader scopes every read by a bound tenant predicate (ported)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg2
import psycopg2.errors
import pytest

from omnimarket.handlers.inference_credentials_http.errors import (
    CredentialReadModelMissingTableError,
    CredentialReadModelNotConfiguredError,
    CredentialReadModelUnavailableError,
)
from omnimarket.handlers.inference_credentials_http.postgres_read_model import (
    ANALYTICS_DSN_SECRET_NAME,
    PostgresInferenceCredentialReadModel,
)
from tests.nodes.inference_credential_http_support import FakeSecretSource

pytestmark = pytest.mark.unit
DSN = "postgresql://role:never-printed@db.example/x"


class _Cursor:
    def __init__(
        self, log: list[Any], rows: list[dict[str, Any]], error: Exception | None
    ) -> None:
        self._log, self._rows, self._error = log, rows, error

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._log.append((sql, params))
        if self._error is not None and "set_config" not in sql:
            raise self._error

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _Connection:
    def __init__(
        self, log: list[Any], rows: list[dict[str, Any]], error: Exception | None
    ) -> None:
        self._args = (log, rows, error)

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def cursor(self, cursor_factory: Any = None) -> _Cursor:
        return _Cursor(*self._args)

    def close(self) -> None:
        return None


def _model(
    rows: list[dict[str, Any]] | None = None,
    error: Exception | None = None,
    dsn: bool = True,
) -> tuple[PostgresInferenceCredentialReadModel, list[Any]]:
    log: list[Any] = []
    source = FakeSecretSource({ANALYTICS_DSN_SECRET_NAME: DSN} if dsn else {})
    return (
        PostgresInferenceCredentialReadModel(
            source, connect=lambda _dsn: _Connection(log, rows or [], error)
        ),
        log,
    )


async def test_list_binds_the_tenant_and_sets_the_guc() -> None:
    model, log = _model()
    await model.list_for_tenant("tenant-123")
    guc, query = log
    assert guc[1] == ("tenant-123",)
    sql, params = query
    assert "WHERE tenant_id = %(tenant)s" in sql
    assert params == {"tenant": "tenant-123"}
    assert "tenant-123" not in sql


async def test_ownership_lookup_binds_ref_and_tenant() -> None:
    model, log = _model()
    assert await model.owned_by_tenant("cred_victim_ref", "attacker") is None
    sql, params = log[1]
    assert "WHERE api_key_ref = %(ref)s AND tenant_id = %(tenant)s" in sql
    assert params == {"ref": "cred_victim_ref", "tenant": "attacker"}
    assert "cred_victim_ref" not in sql
    assert "attacker" not in sql


async def test_an_owned_row_is_returned() -> None:
    row = {
        "api_key_ref": "r",
        "name": "n",
        "provider": "openai",
        "created_at": datetime.now(UTC),
        "revoked_at": None,
    }
    model, _ = _model(rows=[row])
    assert await model.owned_by_tenant("r", "t") == row


async def test_an_undefined_table_is_typed() -> None:
    model, _ = _model(error=psycopg2.errors.UndefinedTable("relation does not exist"))
    with pytest.raises(CredentialReadModelMissingTableError):
        await model.list_for_tenant("t")


async def test_an_unset_dsn_is_not_configured() -> None:
    model, log = _model(dsn=False)
    with pytest.raises(CredentialReadModelNotConfiguredError):
        await model.list_for_tenant("t")
    assert log == []


async def test_other_database_errors_do_not_leak_the_dsn() -> None:
    model, _ = _model(error=psycopg2.OperationalError(f"connection failed: {DSN}"))
    with pytest.raises(CredentialReadModelUnavailableError) as excinfo:
        await model.list_for_tenant("t")
    assert "never-printed" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
