# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""workspace_id on real Postgres (OMN-20967).

Failure modes, each with a test below:
  W1  migration 0008 did not create the column, so the projection's INSERT
      fails on a real database while the mocked unit test passes;
  W2  a re-delivery without the field drops the workspace a key was
      registered with, and the key stops being answered by the provider;
  W3  a key registered without a workspace projects as NULL and sends no
      workspace header, as before.

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

_WORKSPACE = "wrkspc_01SyntheticWorkspaceId"


async def _workspace_id(admin_conn: asyncpg.Connection, ref: str) -> object:
    row = await admin_conn.fetchrow(
        "SELECT workspace_id FROM tenant_inference_credentials WHERE api_key_ref = $1",
        ref,
    )
    assert row is not None
    return row["workspace_id"]


@pytest.mark.integration
async def test_tenant_credentials_workspace_id_w1_is_stored() -> None:
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        ref = f"cred_localinstall_anthropic_{uuid4().hex}"
        assert await runner.project_event(
            TOPIC_REGISTERED,
            _local_registered(ref, provider="anthropic", workspace_id=_WORKSPACE),
            _meta(ref, TOPIC_REGISTERED),
        )
        assert await _workspace_id(admin_conn, ref) == _WORKSPACE


@pytest.mark.integration
async def test_tenant_credentials_workspace_id_w2_survives_a_redelivery() -> None:
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        ref = f"cred_localinstall_anthropic_{uuid4().hex}"
        for workspace in (_WORKSPACE, None):
            await runner.project_event(
                TOPIC_REGISTERED,
                _local_registered(ref, provider="anthropic", workspace_id=workspace),
                _meta(ref, TOPIC_REGISTERED),
            )
        assert await _workspace_id(admin_conn, ref) == _WORKSPACE


@pytest.mark.integration
async def test_tenant_credentials_workspace_id_w3_unset_is_null() -> None:
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        ref = f"cred_localinstall_openrouter_{uuid4().hex}"
        assert await runner.project_event(
            TOPIC_REGISTERED, _local_registered(ref), _meta(ref, TOPIC_REGISTERED)
        )
        assert await _workspace_id(admin_conn, ref) is None
