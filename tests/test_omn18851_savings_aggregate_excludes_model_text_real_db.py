# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18851: migration 090 against a real Postgres.

WHY A REAL DATABASE AND NOT THE STATIC RATCHET NEXT DOOR

``test_omn18851_savings_aggregate_size_ratchet.py`` reads the migration text
and encodes a hand-built row. That is fast and it runs anywhere, but every
claim it makes is about a STRING. Three of this change's claims are claims
about SQL semantics that a text match cannot reach:

* that ``CREATE OR REPLACE VIEW`` ACCEPTS the redefinition at all. Postgres
  refuses a rename, a retype or a reorder of a view's output columns, so
  reaching any assertion below is itself the proof that the column contract
  survived -- the reason migration 090 needs no DROP, and therefore keeps its
  grants and its ``security_invoker`` setting;
* that the aggregate's ELEMENTS actually lose the two keys. ``to_jsonb`` of a
  CTE emits whatever that CTE projects, and only the database can say what
  that is after the join chain and the window function above it;
* that removing them from ``limited_sessions`` changes NOTHING ELSE about the
  row -- the totals, the session count and the ordering are computed from
  ``combined_sessions``, which still carries the text.

WHY THE POSITIVE CONTROL IS NOT OPTIONAL

Every assertion that a key is ABSENT is one typo away from passing for the
wrong reason: a seed that inserted nothing, a tenant filter that matched
nothing, a view that returned no row. So each absence assertion here runs
against a row that is first proven non-empty, and the text the aggregate no
longer carries is proven STILL PRESENT on ``delegation_events`` in the same
transaction. "Excluded from the aggregate" and "gone from the database" look
identical from an empty result, and the difference between them is whether
the per-correlation evidence route still has anything to serve.

WHERE THE REAL DATABASE COMES FROM

Under ``@pytest.mark.integration`` on the repo's shared ``postgres_fixture``,
whose DSN is built from ``INTEGRATION_POSTGRES_HOST`` /
``INTEGRATION_POSTGRES_PORT`` / ``INTEGRATION_POSTGRES_USER`` /
``INTEGRATION_POSTGRES_DB`` plus ``POSTGRES_PASSWORD`` (tests/conftest.py).
Named explicitly because this diff touches a projection write-path file, and
OMN-15909 requires such a diff to carry real-database coverage.

BOTH NODES' MIGRATIONS ARE APPLIED

The view reads ``delegation_events``, which belongs to
``node_projection_delegation``. The chain is applied from disk in the runner's
order rather than stood in for, so it cannot drift from the owning node.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest

_NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
_MIGRATION_NODES = ("node_projection_delegation", "node_projection_savings")

_CONCURRENT_INDEX = re.compile(r"\bCREATE\s+INDEX\s+CONCURRENTLY\b", re.IGNORECASE)
_PUBLIC_SCHEMA = re.compile(r"\bpublic\.", re.IGNORECASE)

_ENSURE_ROLES = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_dashboard') THEN
        CREATE ROLE app_dashboard;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'tenant_projection_writer'
    ) THEN
        CREATE ROLE tenant_projection_writer WITH NOLOGIN NOSUPERUSER
            NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END$$;
"""

SAVINGS = "projection_delegation_savings"

TENANT_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-4222-8222-222222222222")

RUN_A = "aaaaaaaa-0000-4000-8000-00000000000a"
RUN_B = "bbbbbbbb-0000-4000-8000-00000000000b"

_WHEN = datetime(2026, 9, 19, 20, 20, tzinfo=UTC)

#: Deliberately large and deliberately DISTINCTIVE. Large, so that if the
#: aggregate did still embed it the row would be visibly inflated rather than
#: marginally so. Distinctive, so an assertion can search the whole serialized
#: row for the content itself and not merely for a key name -- a future change
#: that renamed the column while still embedding its value would defeat a
#: key-only check.
_RESPONSE_SENTINEL = "RESPONSE-SENTINEL-" + ("z" * 20_000)
_PROMPT_SENTINEL = "PROMPT-SENTINEL-" + ("y" * 5_000)


async def _apply(conn: asyncpg.Connection) -> None:
    for node in _MIGRATION_NODES:
        for migration in sorted(
            (_NODES / node / "migrations").glob("*.sql"), key=lambda p: p.name
        ):
            sql = _CONCURRENT_INDEX.sub("CREATE INDEX", migration.read_text("utf-8"))
            sql = _PUBLIC_SCHEMA.sub("", sql)
            try:
                await conn.execute(sql)
            except Exception as exc:  # pragma: no cover - failure path
                raise AssertionError(
                    f"migration {migration.name} failed: {exc}"
                ) from exc


async def _seed(conn: asyncpg.Connection) -> None:
    """Two tenants, both carrying prompt and response text.

    Two rather than one so an assertion cannot pass because the view collapsed
    every tenant into a single row -- the failure mode migration 089 exists to
    prevent, and one this change must not reintroduce.
    """
    await conn.execute(
        """
        INSERT INTO delegation_events (
            correlation_id, session_id, tenant_id, task_type, delegated_to,
            model_name, quality_gate_passed, cost_usd, cost_savings_usd,
            tokens_input, tokens_output, timestamp, created_at,
            prompt_text, response_text
        ) VALUES
          ($1, $1, $2, 'code_generation', 'local', 'qwen2.5-coder', TRUE,
           0.010000, 0.990000, 100, 200, $5, $5, $6, $7),
          ($3, $3, $4, 'document', 'local', 'gemini-2.5-flash', FALSE,
           0.020000, 0.330000, 10, 20, $5, $5, $6, $7)
        """,
        RUN_A,
        TENANT_A,
        RUN_B,
        TENANT_B,
        _WHEN,
        _PROMPT_SENTINEL,
        _RESPONSE_SENTINEL,
    )
    await conn.execute(
        """
        INSERT INTO savings_estimates (
            event_timestamp, session_id, model_local, model_cloud_baseline,
            local_cost_usd, cloud_cost_usd, savings_usd, tenant_id,
            task_type, prompt_tokens, completion_tokens
        ) VALUES ($1, $2, 'qwen2.5-coder', 'claude-opus-4.1',
                  $3, $4, $5, $6, 'code_generation', 100, 200)
        """,
        _WHEN,
        RUN_A,
        Decimal("0.010000"),
        Decimal("0.780000"),
        Decimal("0.770000"),
        str(TENANT_A),
    )


@pytest.fixture
async def views(postgres_fixture: asyncpg.Connection) -> Any:
    conn = postgres_fixture
    schema = f"omn18851_{uuid.uuid4().hex[:12]}"
    await conn.execute(_ENSURE_ROLES)
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}", public')
    try:
        await _apply(conn)
        await _seed(conn)
        yield conn
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


async def _row(conn: asyncpg.Connection, tenant: uuid.UUID) -> Any:
    return await conn.fetchrow(
        f"SELECT * FROM {SAVINGS} WHERE tenant_id = $1", str(tenant)
    )


def _sessions(row: Any) -> list[dict[str, Any]]:
    payload = row["sessions"]
    return list(json.loads(payload) if isinstance(payload, str) else payload)


@pytest.mark.integration
@pytest.mark.parametrize("tenant", [TENANT_A, TENANT_B])
async def test_sessions_elements_carry_no_model_text(
    views: asyncpg.Connection, tenant: uuid.UUID
) -> None:
    """The aggregate's elements must not carry either text column.

    Reaching this assertion at all is the proof that migration 090's
    ``CREATE OR REPLACE VIEW`` was accepted, which is the proof that the
    output column contract is unchanged.
    """
    row = await _row(views, tenant)
    assert row is not None, f"positive control: {SAVINGS} returns a row for {tenant}"
    sessions = _sessions(row)
    assert sessions, "positive control: the sessions array is non-empty"

    for element in sessions:
        assert "prompt_text" not in element, (
            f"prompt_text is still embedded in the sessions aggregate: {sorted(element)}"
        )
        assert "response_text" not in element, (
            f"response_text is still embedded in the sessions aggregate: "
            f"{sorted(element)}"
        )


@pytest.mark.integration
async def test_the_text_content_itself_is_absent_from_the_serialized_row(
    views: asyncpg.Connection,
) -> None:
    """Search the whole serialized row for the VALUES, not just the key names.

    A rename that kept embedding the same text would pass a key-only check and
    reproduce the outage under a different column name.
    """
    row = await _row(views, TENANT_A)
    assert row is not None
    serialized = json.dumps(_sessions(row))
    assert "RESPONSE-SENTINEL" not in serialized
    assert "PROMPT-SENTINEL" not in serialized


@pytest.mark.integration
async def test_positive_control_the_text_is_still_on_delegation_events(
    views: asyncpg.Connection,
) -> None:
    """The absence above must be exclusion, not deletion.

    The per-correlation evidence route reads these columns straight off
    ``delegation_events``. If this control ever fails, the change stopped being
    "do not embed the artifact" and became "throw the artifact away".
    """
    record = await views.fetchrow(
        "SELECT prompt_text, response_text FROM delegation_events "
        "WHERE correlation_id = $1",
        RUN_A,
    )
    assert record is not None, "positive control: the delegation row exists"
    assert record["response_text"] == _RESPONSE_SENTINEL
    assert record["prompt_text"] == _PROMPT_SENTINEL


@pytest.mark.integration
async def test_the_aggregate_is_dramatically_smaller_than_the_text_it_dropped(
    views: asyncpg.Connection,
) -> None:
    """A size claim measured by the database, not by a hand-built fixture.

    The seeded text alone is over 25 KB per delegation. If the aggregate were
    still embedding it the serialized array could not possibly come in under
    that, so this is a real discriminator rather than a restatement of the
    key-absence assertions above.
    """
    row = await _row(views, TENANT_A)
    assert row is not None
    serialized_bytes = len(json.dumps(_sessions(row)).encode("utf-8"))
    seeded_text_bytes = len(_RESPONSE_SENTINEL) + len(_PROMPT_SENTINEL)
    assert serialized_bytes < seeded_text_bytes, (
        f"the aggregate serialized to {serialized_bytes:,} bytes for one "
        f"session, which is not smaller than the {seeded_text_bytes:,} bytes "
        f"of text seeded onto that delegation -- the text is still in there"
    )


@pytest.mark.integration
async def test_dropping_the_text_did_not_disturb_the_rest_of_the_row(
    views: asyncpg.Connection,
) -> None:
    """The totals are computed from ``combined_sessions``, which still carries
    the text; only the aggregate's input CTE changed. If a total moved, the
    edit reached further than intended."""
    row = await _row(views, TENANT_A)
    assert row is not None
    assert row["session_count"] == 1
    # savings_estimates wins the union's precedence for a session present in
    # both sources, exactly as before 090.
    assert float(row["cumulative_savings_usd"]) == pytest.approx(0.77)
    assert float(row["cumulative_local_cost_usd"]) == pytest.approx(0.01)
    assert float(row["cumulative_cloud_cost_usd"]) == pytest.approx(0.78)

    session = _sessions(row)[0]
    # The fields a reader actually renders are all still present.
    for field in (
        "session_id",
        "task_type",
        "model_name",
        "local_cost_usd",
        "cloud_cost_usd",
        "savings_usd",
        "savings_method",
        "usage_source",
        "prompt_tokens",
        "completion_tokens",
        "created_at",
    ):
        assert field in session, f"{field} disappeared from the sessions element"


@pytest.mark.integration
async def test_tenants_still_get_their_own_rows(views: asyncpg.Connection) -> None:
    """089's per-tenant grouping must survive 090.

    One tenant cannot distinguish a correctly grouped view from an ungrouped
    one, so both are read and their session ids compared.
    """
    row_a = await _row(views, TENANT_A)
    row_b = await _row(views, TENANT_B)
    assert row_a is not None
    assert row_b is not None
    assert [s["session_id"] for s in _sessions(row_a)] == [RUN_A]
    assert [s["session_id"] for s in _sessions(row_b)] == [RUN_B]
