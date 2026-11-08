# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22030: real-Postgres proof that the projection writer may delete a retired service's row.

The writer's role held SELECT, INSERT and UPDATE on ``local_environment``; a retired report deletes
a row, which only a grant and the tenant policy decide, so only a real database proves it. SKIPS
without a reachable database (INTEGRATION_POSTGRES_PASSWORD and the INTEGRATION_POSTGRES_HOST, _PORT, _USER and
_DB settings the sibling proofs read) and uses its own throwaway schema and role.

Failure modes each test is written against:

* the migration is absent, so the writer's delete is refused with a permission error;
* the delete reaches another tenant's row of the same service;
* the migration does not apply twice.
"""

from __future__ import annotations

import asyncpg
import pytest

from tests.test_omn19988_local_environment_migrations_real_postgres import (
    _MIGRATIONS_DIR,
    _TENANT_A,
    _TENANT_B,
    _connect_or_skip,
    _drop_role,
    _fresh,
    _insert,
    _row,
    _scoped,
    _writer_role,
)

_SCHEMA = "omn22030_local_environment_delete_test"
_ROLE = "omn22030_local_environment_delete_probe"
_MIGRATION = _MIGRATIONS_DIR / "0003_grant_delete_local_environment.sql"


@pytest.mark.integration
async def test_real_postgres_the_writer_deletes_only_its_tenants_retired_row() -> None:
    conn = await _connect_or_skip()
    try:
        await _fresh(conn, _SCHEMA)
        await _writer_role(conn, _SCHEMA, _ROLE)
        await _insert(conn, _SCHEMA, _row(_TENANT_B, "runtime-effects"))

        await conn.execute(f"SET ROLE {_ROLE}")
        await conn.fetchval("SELECT set_config('app.tenant_id', $1, false)", _TENANT_A)
        await _insert(conn, _SCHEMA, _row(_TENANT_A, "runtime-effects"))
        delete = (
            f"DELETE FROM {_SCHEMA}.local_environment "
            "WHERE tenant_id = $1 AND mode = 'compose' AND service = $2"
        )
        # Failing control: without the migration the role may not delete.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(delete, _TENANT_A, "runtime-effects")
        await conn.execute("RESET ROLE")

        # Twice: the migration is re-runnable over its own result.
        scoped = _scoped(_MIGRATION.read_text(encoding="utf-8"), _SCHEMA).replace(
            "tenant_projection_writer", _ROLE
        )
        await conn.execute(scoped)
        await conn.execute(scoped)

        await conn.execute(f"SET ROLE {_ROLE}")
        await conn.fetchval("SELECT set_config('app.tenant_id', $1, false)", _TENANT_A)
        assert await conn.execute(delete, _TENANT_A, "runtime-effects") == "DELETE 1"
        # The other tenant's row of the same service is outside the policy.
        assert await conn.execute(delete, _TENANT_B, "runtime-effects") == "DELETE 0"
        await conn.execute("RESET ROLE")
        remaining = await conn.fetch(
            f"SELECT tenant_id FROM {_SCHEMA}.local_environment"
        )
        assert [r["tenant_id"] for r in remaining] == [_TENANT_B]
    finally:
        await conn.execute("RESET ROLE")
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await _drop_role(conn, _ROLE)
        await conn.close()
