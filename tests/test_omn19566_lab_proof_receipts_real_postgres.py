# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19566: real-Postgres write-path proof for the lab_proof_receipts projection.

The writer tests drive the statements against a recording double, which binds
any Python type and runs no SQL. The ``::jsonb`` casts, the five-column conflict
target and the ``finished_at`` ordering guard exist only in SQL, so a real
database is the only place they can be proven. Mirrors
tests/test_omn19716_topic_activity_real_postgres.py: it SKIPS without a reachable
database and uses its own throwaway schema.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.nodes.node_projection_lab_proof_receipts.handlers.handler_lab_proof_receipts_writer import (
    _SELECT_PRIOR,
    _UPSERT,
    _bind,
    _prior_row,
)
from omnimarket.nodes.node_projection_lab_proof_receipts.handlers.handler_projection_lab_proof_receipts import (
    HandlerProjectionLabProofReceipts,
)
from omnimarket.nodes.node_projection_lab_proof_receipts.models import (
    ModelLabProofReceiptEvent,
    ModelLabProofReceiptProjectionRequest,
    ModelLabProofReceiptRow,
)

_NODE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_lab_proof_receipts"
)
_MIGRATION = _NODE / "migrations" / "0000_create_lab_proof_receipts.sql"
_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "lab_proof_receipts"
    / "omnibase_infra-4217-event.json"
)
_SCHEMA = "omn19566_lab_proof_receipts_write_path_test"


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping lab_proof_receipts write-path DB proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (
        OSError,
        asyncpg.PostgresError,
    ) as exc:  # pragma: no cover - infra-dependent
        pytest.skip(
            f"no reachable Postgres for lab_proof_receipts write-path proof: {exc}"
        )


def _scoped(statement: str) -> str:
    return statement.replace("omninode_internal.", f"{_SCHEMA}.")


def _row(**changes: Any) -> ModelLabProofReceiptRow:
    event = ModelLabProofReceiptEvent.model_validate(
        json.loads(_FIXTURE.read_text(encoding="utf-8"))
    )
    result = HandlerProjectionLabProofReceipts().handle(
        ModelLabProofReceiptProjectionRequest(event=event)
    )
    return result.row.model_copy(update=changes)


async def _setup(conn: asyncpg.Connection) -> None:
    await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    await conn.execute(_scoped(_MIGRATION.read_text(encoding="utf-8")))


@pytest.mark.integration
async def test_real_postgres_stores_the_producers_receipt_and_reads_it_back() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        row = _row()
        returned = await conn.fetch(_scoped(_UPSERT), *_bind(row))
        assert len(returned) == 1
        assert returned[0]["receipt_key"] == row.receipt_key
        assert isinstance(returned[0]["projection_cursor"], int)
        prior = await conn.fetch(
            _scoped(_SELECT_PRIOR),
            row.repo,
            row.pr_number,
            row.head_sha,
            row.profile_id,
            row.profile_version,
        )
        # jsonb comes back as text through asyncpg; _prior_row decodes it into
        # the same row the fold produced
        assert _prior_row(dict(prior[0])) == row
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_refuses_an_older_proof_of_the_same_key() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        row = _row()
        assert await conn.fetch(_scoped(_UPSERT), *_bind(row))
        older = _row(
            finished_at=row.finished_at - timedelta(minutes=1),
            verifier_token="ACCEPTED",
        )
        assert await conn.fetch(_scoped(_UPSERT), *_bind(older)) == []
        assert (
            await conn.fetchval(
                f"SELECT verifier_token FROM {_SCHEMA}.lab_proof_receipts "
                "WHERE receipt_key = $1",
                row.receipt_key,
            )
            == row.verifier_token
        )
        # positive control: a newer proof of the key replaces the row
        newer = _row(
            finished_at=row.finished_at + timedelta(minutes=30),
            verifier_token="ACCEPTED",
        )
        assert await conn.fetch(_scoped(_UPSERT), *_bind(newer))
        assert (
            await conn.fetchval(
                f"SELECT verifier_token FROM {_SCHEMA}.lab_proof_receipts "
                "WHERE receipt_key = $1",
                row.receipt_key,
            )
            == "ACCEPTED"
        )
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()
