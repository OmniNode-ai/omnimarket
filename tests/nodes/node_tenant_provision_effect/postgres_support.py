# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A scratch PostgreSQL 16 holding the identity tables the tenant provision node writes.

The tables are the dev ``omninode_cloud`` identity tables: ``tenants`` as read from the dev columns, ``plan_tiers`` and
``tenant_plans`` with the active-plan unique index, and ``tenant_event_outbox`` holding the envelope as JSON. The
fixture is CI's provisioned database when ``INTEGRATION_POSTGRES_PASSWORD`` is set, else an owned localhost-only
container; it never reaches a deployed database.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

DATABASE = "omn_tenant_provision"

IDENTITY_DDL = """
DROP SCHEMA IF EXISTS tenant_provision_scratch CASCADE;
CREATE SCHEMA tenant_provision_scratch;
SET search_path TO tenant_provision_scratch;
CREATE TABLE tenants (
    tenant_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_slug text UNIQUE NOT NULL,
    slug text,
    name text,
    created_by_sub text,
    provisioning_state text NOT NULL DEFAULT 'active'
        CHECK (provisioning_state IN ('pending', 'active', 'suspended', 'failed', 'NEEDS_KC_PATCH')),
    kc_patch_attempt_count integer NOT NULL DEFAULT 0,
    kc_patch_last_error text,
    owner_email text,
    principal_id text,
    stripe_customer_id text,
    acl_state text NOT NULL DEFAULT 'PENDING_MSK_RECONCILIATION',
    created_at timestamptz NOT NULL DEFAULT now(),
    quota_state text
);
CREATE UNIQUE INDEX uniq_tenants_created_by_sub ON tenants (created_by_sub) WHERE created_by_sub IS NOT NULL;
CREATE UNIQUE INDEX uniq_tenants_principal_id ON tenants (principal_id) WHERE principal_id IS NOT NULL;
CREATE TABLE plan_tiers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text UNIQUE NOT NULL,
    is_default boolean NOT NULL DEFAULT false
);
CREATE TABLE tenant_plans (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants (tenant_id),
    plan_id uuid NOT NULL REFERENCES plan_tiers (id),
    is_active boolean NOT NULL DEFAULT true,
    started_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uniq_tenant_plans_active ON tenant_plans (tenant_id) WHERE is_active;
CREATE TABLE tenant_event_outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants (tenant_id),
    envelope jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz
);
INSERT INTO plan_tiers (code, is_default) VALUES ('discovery', true), ('team', false);
"""


def connect_kwargs(port: int) -> dict[str, Any]:
    if os.environ.get("INTEGRATION_POSTGRES_PASSWORD"):
        return {
            "user": os.environ.get("INTEGRATION_POSTGRES_USER", "postgres"),
            "database": os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra"),
            "host": os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost"),
            "port": port,
            "password": os.environ["INTEGRATION_POSTGRES_PASSWORD"],
            "ssl": False,
        }
    return {
        "user": "postgres",
        "database": DATABASE,
        "host": "127.0.0.1",
        "port": port,
        "ssl": False,
    }


async def accepts_sql(port: int) -> bool:
    try:
        connection = await asyncpg.connect(**connect_kwargs(port), timeout=1)
    except (OSError, asyncpg.PostgresError):
        return False
    try:
        return await connection.fetchval("SELECT 1") == 1
    finally:
        await connection.close()


@asynccontextmanager
async def identity_tables(
    port: int,
) -> AsyncIterator[tuple[asyncpg.Connection, asyncpg.Pool]]:
    """A fresh set of identity tables, an admin connection to read them back and a pool for the node."""
    settings = {"search_path": "tenant_provision_scratch"}
    admin = await asyncpg.connect(**connect_kwargs(port), server_settings=settings)
    pool = await asyncpg.create_pool(
        **connect_kwargs(port), min_size=1, max_size=2, server_settings=settings
    )
    try:
        await admin.execute(IDENTITY_DDL)
        yield admin, pool
    finally:
        await pool.close()
        await admin.close()
