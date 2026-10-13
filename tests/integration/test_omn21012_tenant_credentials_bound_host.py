# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""bound_host on real Postgres (OMN-21012).

Failure modes, each with a test below:
  B1  migration 0007 did not create the column, so the projection's INSERT
      fails on a real database while the mocked unit test passes;
  B2  a re-delivery without the field unbinds a key that was bound to a host;
  B3  a key registered without a host projects as NULL, usable from any host.

The database is the one the real-Postgres write-path suite provisions from
its INTEGRATION_POSTGRES_* settings; like that suite, these skip when no
database is reachable.
"""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest

from tests.test_omn16316_real_postgres_tenant_credentials_write_path import (
    TOPIC_REGISTERED,
    _local_registered,
    _meta,
    _provisioned_runner,
)


async def _bound_host(admin_conn: asyncpg.Connection, ref: str) -> object:
    row = await admin_conn.fetchrow(
        "SELECT bound_host FROM tenant_inference_credentials WHERE api_key_ref = $1",
        ref,
    )
    assert row is not None
    return row["bound_host"]


@pytest.mark.integration
async def test_tenant_credentials_bound_host_b1_is_stored() -> None:
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        ref = f"cred_localinstall_anthropic_{uuid4().hex}"
        assert await runner.project_event(
            TOPIC_REGISTERED,
            _local_registered(ref, bound_host="h201"),
            _meta(ref, TOPIC_REGISTERED),
        )
        assert await _bound_host(admin_conn, ref) == "h201"


@pytest.mark.integration
async def test_tenant_credentials_bound_host_b2_survives_a_redelivery() -> None:
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        ref = f"cred_localinstall_anthropic_{uuid4().hex}"
        for host in ("h101", None):
            await runner.project_event(
                TOPIC_REGISTERED,
                _local_registered(ref, bound_host=host),
                _meta(ref, TOPIC_REGISTERED),
            )
        assert await _bound_host(admin_conn, ref) == "h101"


@pytest.mark.integration
async def test_tenant_credentials_bound_host_b3_unset_is_null() -> None:
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        ref = f"cred_localinstall_openrouter_{uuid4().hex}"
        assert await runner.project_event(
            TOPIC_REGISTERED, _local_registered(ref), _meta(ref, TOPIC_REGISTERED)
        )
        assert await _bound_host(admin_conn, ref) is None
