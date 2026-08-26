# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15533: the delegation-savings read views must not fabricate task_type,
token counts, or savings provenance.

Three defects on the ``savings_sessions`` CTE -- the only branch that produces rows
on the dev lane, because ``delegation_events`` is empty there (OMN-14153):

1. ``model_local AS task_type`` overwrote the task class with the model name, so a
   delegation whose terminal carried ``task_type=escalation`` rendered
   ``task_type="gemini-2.5-flash"``.
2. ``0::int AS prompt_tokens`` / ``0::int AS completion_tokens`` hardcoded zero
   while the source terminals carried real counts (122/1248, 109/6802).
3. ``'measured' AS savings_method`` stamped a measurement claim on rows whose own
   ``pricing_manifest_version`` is ``savings-estimated`` and whose tokens were the
   hardcoded zero above.

The root cause is one seam, not three: ``savings_estimates`` carried no task class
and no token counts, so the view had nothing else to select. Migration 082 adds the
columns, the projection models carry the values that were already in hand, and
migration 083 repoints both views onto them.

Static migration-shape and write-path assertions run everywhere. The executable
RED->GREEN proof runs on the repo's shared ``postgres_fixture`` under
``@pytest.mark.integration``, so it is gated by the same integration job (and the
same OMN-14172 silent-skip guard) as every other real-database test here rather
than by a bespoke environment variable that would skip unnoticed.

That fixture connects to a REAL Postgres built from ``INTEGRATION_POSTGRES_HOST``
/ ``INTEGRATION_POSTGRES_PORT`` / ``INTEGRATION_POSTGRES_USER`` /
``INTEGRATION_POSTGRES_DB`` plus ``POSTGRES_PASSWORD`` (see tests/conftest.py).
Naming the DSN source here is deliberate: these projection changes touch a
write-path file, and OMN-15909 requires such a diff to carry real-database
coverage rather than mocked adapters, because only a live connection catches the
schema/adapter mismatches (OMN-15905) that an AsyncMock will happily accept.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillSavingsProjection,
    ModelTaskDelegatedSavingsSource,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_projection_savings import (
    HandlerProjectionSavings,
    ModelSavingsEstimatedEvent,
)
from omnimarket.pricing import build_premium_counterfactual
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from tests.constants import MODEL_CLAUDE_OPUS_4_6, MODEL_QWEN3_CODER_30B

# Paths are derived from this file, never from the working directory: pytest is
# routinely invoked from a parent directory and a cwd-relative path would resolve
# to nothing there.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_NODES = _REPO_ROOT / "src" / "omnimarket" / "nodes"
_SAVINGS_NODE = _NODES / "node_projection_savings"
_MIGRATIONS = _SAVINGS_NODE / "migrations"
_CONTRACT = _SAVINGS_NODE / "contract.yaml"

# Apply order across nodes. node_projection_delegation owns delegation_events,
# which both savings views read, so it has to be materialized first; within a node
# the runner applies migrations in lexical filename order.
_MIGRATION_NODES = ("node_projection_delegation", "node_projection_savings")


def _migration(prefix: str) -> Path:
    """Resolve one migration by its numeric prefix.

    Matching on the prefix rather than the full filename means a rename fails
    loudly here instead of silently resolving to a path that no longer exists.
    """
    matches = sorted(_MIGRATIONS.glob(f"{prefix}_*.sql"))
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one migration with prefix {prefix!r}, found {matches}"
        )
    return matches[0]


_BASE_TABLE = _migration("074")
_SAVINGS_VIEW = _migration("076")
_SERIES_VIEW = _migration("078")
_SERIES_TIER_VIEW = _migration("079")
_TOKEN_COLUMNS = _migration("082")
_VIEW_RECONCILE = _migration("083")

# The two migrations this ticket adds. Everything else in the chain is the
# pre-fix world, which is what the RED phase applies.
_FIX_MIGRATIONS = (_TOKEN_COLUMNS, _VIEW_RECONCILE)

# Migrations that shipped the defect. run-projection-migrations.py records a
# sha256 per applied file and refuses to continue when one changes, so these stay
# frozen and 083 corrects them forward.
_FROZEN_DEFECTIVE_MIGRATIONS = (_SAVINGS_VIEW, _SERIES_VIEW, _SERIES_TIER_VIEW)

# Live evidence from the 13-class matrix rerun (correlation ...013 for the class
# mismatch, ...001 for the token counts). The serving model is carried verbatim
# from the ticket rather than via tests.constants: the recorded evidence is
# gemini-2.5-flash, which is a different model from the constants'
# MODEL_GEMINI_2_5_FLASH_LITE, and substituting it would stop this reproducing
# the reported row.
_TASK_TYPE = "escalation"
_MODEL_NAME = "gemini-2.5-flash"
_PROMPT_TOKENS = 122
_COMPLETION_TOKENS = 1248
_CORRELATION_ID = "7a300730-0000-4000-8000-000000000013"
_EVENT_TIMESTAMP = datetime(2026, 7, 30, 18, 46, tzinfo=UTC)

# CREATE INDEX CONCURRENTLY cannot run inside a transaction block, and asyncpg
# sends a multi-statement migration as one. CONCURRENTLY is a production
# availability optimization -- it avoids locking a live table against writes --
# and is meaningless against a throwaway schema no other session can see, so the
# index is built plainly here. The rewrite is deliberately narrow: it changes how
# the index is built, never whether it exists or what it covers.
_CONCURRENT_INDEX = re.compile(r"\bCREATE\s+INDEX\s+CONCURRENTLY\b", re.IGNORECASE)
_PUBLIC_SCHEMA = re.compile(r"\bpublic\.", re.IGNORECASE)

# The RLS migrations GRANT to this role, which omnibase_infra's forward migration
# 094 owns. It lives in a different repository, so the role is created here rather
# than cross-importing a migration this repo does not govern.
_ENSURE_DASHBOARD_ROLE = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_dashboard') THEN
        CREATE ROLE app_dashboard;
    END IF;
END$$;
"""

_INSERT_SAVINGS_ROW = """
INSERT INTO savings_estimates (
    event_timestamp, session_id, model_local, model_cloud_baseline,
    local_cost_usd, cloud_cost_usd, savings_usd
) VALUES ($1, $2, $3, $4, $5, $6, $7);
"""

# Money stays Decimal end to end, as it is across the projection models: a float
# literal here would not round-trip through NUMERIC(18, 6) exactly and could break
# the savings_usd == cloud - local invariant the models enforce.
_LOCAL_COST_USD = Decimal("0.000000")
_COUNTERFACTUAL_USD = Decimal("0.021975")


def _migration_chain() -> tuple[Path, ...]:
    """Every migration the two views depend on, in the runner's apply order."""
    chain: list[Path] = []
    for node in _MIGRATION_NODES:
        chain.extend(
            sorted((_NODES / node / "migrations").glob("*.sql"), key=lambda p: p.name)
        )
    return tuple(chain)


def _sql_body(path: Path) -> str:
    """Executable SQL only -- ``--`` comments stripped.

    Each migration header names the defects it removes, so a negative assertion
    against the raw text would match the explanation instead of the code.
    """
    return "\n".join(
        line.split("--", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()
    )


def _savings_view_body(body: str) -> str:
    """The ``projection_delegation_savings`` half of migration 083.

    Fails loudly when the marker is absent rather than raising a bare IndexError,
    so a renamed view reports what it could not find — the same convention
    ``_migration`` uses for an unresolvable prefix.
    """
    marker = "CREATE OR REPLACE VIEW public.projection_delegation_savings AS"
    parts = body.split(marker, 1)
    if len(parts) != 2:
        raise AssertionError(f"{_VIEW_RECONCILE.name} does not declare {marker!r}")
    return parts[1].split(
        "CREATE OR REPLACE VIEW public.projection_delegation_savings_series AS", 1
    )[0]


@pytest.mark.unit
class TestMigrationOrdering:
    """Apply order is lexical filename order (run-projection-migrations.py)."""

    def test_columns_land_before_the_view_that_selects_them(self) -> None:
        assert _BASE_TABLE.name < _TOKEN_COLUMNS.name < _VIEW_RECONCILE.name
        for defective in _FROZEN_DEFECTIVE_MIGRATIONS:
            assert defective.name < _TOKEN_COLUMNS.name

    def test_view_rebuild_immediately_follows_the_column_add(self) -> None:
        # Nothing may be inserted between the column add and the view that reads
        # the columns, or a fresh-database apply fails on a missing column.
        # Asserted as RELATIVE order, not chain position: a later 084+ migration
        # is expected and must not fail this, since the ordering guarantee still
        # holds.
        savings_chain = [p for p in _migration_chain() if p.parent == _MIGRATIONS]
        columns_index = savings_chain.index(_TOKEN_COLUMNS)
        assert savings_chain[columns_index + 1] == _VIEW_RECONCILE

    def test_applied_migrations_are_not_edited_in_place(self) -> None:
        # Changing an applied migration raises "Checksum mismatch for
        # already-applied migration ... Schema drift detected" and halts the
        # runner on every migrated database, including the dev lane. The defect
        # must therefore remain visible in the frozen files; 083 is the fix. A
        # failure here means someone edited a frozen migration.
        for defective in _FROZEN_DEFECTIVE_MIGRATIONS:
            assert "model_local AS task_type" in _sql_body(defective)


@pytest.mark.unit
class TestSavingsEstimatesTokenColumns:
    """AC1/AC2 need somewhere to put the truth: savings_estimates carried neither
    a task class nor a token count, which is why the view substituted."""

    def test_migration_adds_task_type_and_token_columns(self) -> None:
        body = _sql_body(_TOKEN_COLUMNS)
        for column in (
            "task_type TEXT",
            "prompt_tokens INTEGER",
            "completion_tokens INTEGER",
        ):
            assert f"ADD COLUMN IF NOT EXISTS {column}" in body

    def test_columns_are_nullable_with_no_manufactured_default(self) -> None:
        # A row written before this migration has no recorded class or count.
        # DEFAULT '' or DEFAULT 0 would manufacture an observation that never
        # happened -- the same defect as ``0::int AS prompt_tokens``.
        body = _sql_body(_TOKEN_COLUMNS)
        for column in ("task_type", "prompt_tokens", "completion_tokens"):
            assert f"{column} SET NOT NULL" not in body
        assert "DEFAULT 0" not in body
        assert "DEFAULT ''" not in body

    def test_negative_token_counts_are_rejected(self) -> None:
        body = _sql_body(_TOKEN_COLUMNS)
        assert "prompt_tokens IS NULL OR prompt_tokens >= 0" in body
        assert "completion_tokens IS NULL OR completion_tokens >= 0" in body


@pytest.mark.unit
class TestProjectionViewReconciliation:
    """AC1/AC2/AC3/AC4/AC5 at the SQL layer."""

    def test_reconciles_both_views(self) -> None:
        # AC5: the alias appears in 076 (projection_delegation_savings) and in
        # 078/079 (projection_delegation_savings_series). Fixing only the first
        # leaves the series view fabricating.
        body = _sql_body(_VIEW_RECONCILE)
        assert "CREATE OR REPLACE VIEW public.projection_delegation_savings AS" in body
        assert (
            "CREATE OR REPLACE VIEW public.projection_delegation_savings_series AS"
            in body
        )

    def test_task_type_is_never_aliased_from_the_model_name(self) -> None:
        # AC1 -- the whole ticket.
        body = _sql_body(_VIEW_RECONCILE)
        assert "model_local AS task_type" not in body
        # Both branches of both views read the real column.
        assert body.count("COALESCE(task_type, '') AS task_type") == 4

    def test_token_counts_are_not_hardcoded(self) -> None:
        # AC2.
        body = _sql_body(_VIEW_RECONCILE)
        assert "0::int AS prompt_tokens" not in body
        assert "0::int AS completion_tokens" not in body
        assert "prompt_tokens::int AS prompt_tokens" in body
        assert "completion_tokens::int AS completion_tokens" in body

    def test_savings_method_is_never_an_unconditional_literal(self) -> None:
        # AC3: with provenance keyed on served tokens, the falsifying conjunction
        # (measured + savings-estimated + zero tokens) is unreachable by
        # construction rather than merely absent from the fixtures.
        body = _sql_body(_VIEW_RECONCILE)
        assert "'measured' AS savings_method" not in body
        assert (
            body.count(
                "CASE WHEN COALESCE(prompt_tokens, 0) "
                "+ COALESCE(completion_tokens, 0) > 0"
            )
            == 4
        )

    def test_usage_source_matches_the_consumer_contract(self) -> None:
        # The savings branch emitted the literal 'savings_estimates', which is not
        # one of the three values omnidash's delegation-savings.types.ts declares.
        body = _sql_body(_VIEW_RECONCILE)
        assert "'savings_estimates' AS usage_source" not in body
        assert "THEN 'measured' ELSE 'unknown' END AS usage_source" in body

    def test_counterfactual_baseline_is_separately_named(self) -> None:
        # AC4: cumulative_cloud_cost_usd holds the pinned counterfactual, not cloud
        # spend. CREATE OR REPLACE VIEW cannot rename an existing output column, so
        # the honest name is appended carrying the same value.
        body = _sql_body(_VIEW_RECONCILE)
        assert "AS counterfactual_baseline_usd" in body
        assert "AS cumulative_counterfactual_baseline_usd" in body

    def test_preexisting_output_columns_are_preserved(self) -> None:
        # CREATE OR REPLACE VIEW can only append: a dropped, renamed, retyped or
        # reordered output column makes the migration fail to apply.
        body = _sql_body(_VIEW_RECONCILE)
        savings = _savings_view_body(body)
        for column in (
            "totals.cumulative_savings_usd",
            "totals.cumulative_local_cost_usd",
            "totals.cumulative_cloud_cost_usd",
            "AS baseline_model",
            "AS pricing_manifest_version",
            "totals.session_count",
            "sessions.rows AS sessions",
            "AS captured_at",
            "TRUE AS provisioned",
            "totals.latest_projection_updated_at",
        ):
            assert column in savings
        for column in (
            "AS bucket",
            "AS actual_cost_usd",
            "AS baseline_cost_usd",
            "AS savings_usd",
            "AS task_count",
            "AS local_pct",
            "AS cheap_pct",
            "AS prem_pct",
        ):
            assert column in body

    def test_tier_mix_machinery_is_preserved(self) -> None:
        # 083 must not re-litigate OMN-13661: the authoritative tier mapping and
        # the tier-routed denominator carry over untouched.
        body = _sql_body(_VIEW_RECONCILE)
        assert "WHEN cost_tier_name = 'local' THEN 'local'" in body
        assert (
            "WHEN cost_tier_name IN ('cheap_cloud', 'cheap_frontier') THEN 'cheap'"
            in body
        )
        assert "WHEN cost_tier_name = 'claude' THEN 'premium'" in body
        assert "NULLIF(COUNT(*) FILTER (WHERE tier_bucket IS NOT NULL), 0)" in body

    def test_contract_exposes_the_appended_column(self) -> None:
        contract = yaml.safe_load(_CONTRACT.read_text())
        exposure = next(
            e
            for e in contract["projection_api"]["exposures"]
            if e["table"] == "projection_delegation_savings"
        )
        # Appended last, mirroring the view's column order.
        assert exposure["columns"][-1] == "cumulative_counterfactual_baseline_usd"


@pytest.mark.unit
class TestWritePathCarriesTaskTypeAndTokens:
    """The values were never missing upstream -- they were dropped at this seam."""

    def test_canonical_terminal_carries_task_class_and_counterfactual_tokens(
        self,
    ) -> None:
        source = ModelTaskDelegatedSavingsSource.from_canonical_payload(
            {
                "correlation_id": _CORRELATION_ID,
                "task_type": _TASK_TYPE,
                "model_used": _MODEL_NAME,
                "quality_passed": True,
                "cumulative_attempt_cost": float(_LOCAL_COST_USD),
                "cumulative_input_tokens": _PROMPT_TOKENS,
                "cumulative_output_tokens": _COMPLETION_TOKENS,
                "timestamp": _EVENT_TIMESTAMP,
            },
            counterfactual_builder=build_premium_counterfactual,
        )
        projection = ModelDelegateSkillSavingsProjection.from_task_delegated_event(
            source, baseline_model=MODEL_CLAUDE_OPUS_4_6
        )
        assert projection is not None
        # AC1: the task class and the model name are two distinct values.
        assert projection.task_type == _TASK_TYPE
        assert projection.model_local == _MODEL_NAME
        # AC2: the counts the counterfactual cost was derived from, so a verifier
        # can recompute the saving from the persisted row.
        assert projection.prompt_tokens == _PROMPT_TOKENS
        assert projection.completion_tokens == _COMPLETION_TOKENS

    def test_delegate_skill_terminal_row_carries_all_three(self) -> None:
        db = InmemoryDatabaseAdapter()
        HandlerProjectionSavings().handle(
            {
                "_db": db,
                "_event_type": "delegate-skill-completed",
                "status": "completed",
                "correlation_id": "f9243395-5cb6-4036-8ffb-39dd25547413",
                "task_type": "document",
                "provider": "local-qwen",
                "model_name": MODEL_QWEN3_CODER_30B,
                "quality_gate_passed": True,
                "metrics": {
                    "input_tokens": _PROMPT_TOKENS,
                    "output_tokens": _COMPLETION_TOKENS,
                    "total_tokens": _PROMPT_TOKENS + _COMPLETION_TOKENS,
                    "cost_usd": 0.0,
                    "cost_savings_usd": 0.006003,
                    "latency_ms": 900,
                },
            }
        )
        row = db.query("savings_estimates")[0]
        assert row["task_type"] == "document"
        assert row["task_type"] != row["model_local"]
        assert row["prompt_tokens"] == _PROMPT_TOKENS
        assert row["completion_tokens"] == _COMPLETION_TOKENS

    def test_event_without_counts_writes_no_substitute(self) -> None:
        # The real savings-estimated.v1 producer carries neither. Omitting the keys
        # leaves the columns NULL ("not recorded") rather than stamping 0, which the
        # view reads as the provenance claim "no tokens served".
        db = InmemoryDatabaseAdapter()
        HandlerProjectionSavings().project(
            ModelSavingsEstimatedEvent(
                event_timestamp=_EVENT_TIMESTAMP,
                session_id="sess-no-counts",
                model_local=MODEL_QWEN3_CODER_30B,
                model_cloud_baseline=MODEL_CLAUDE_OPUS_4_6,
                local_cost_usd=_LOCAL_COST_USD,
                cloud_cost_usd=_COUNTERFACTUAL_USD,
                savings_usd=_COUNTERFACTUAL_USD,
            ),
            db,
        )
        row = db.query("savings_estimates")[0]
        assert "task_type" not in row
        assert "prompt_tokens" not in row
        assert "completion_tokens" not in row

    def test_event_passes_through_counts_when_supplied(self) -> None:
        db = InmemoryDatabaseAdapter()
        HandlerProjectionSavings().project(
            ModelSavingsEstimatedEvent(
                event_timestamp=_EVENT_TIMESTAMP,
                session_id="sess-with-counts",
                model_local=MODEL_QWEN3_CODER_30B,
                model_cloud_baseline=MODEL_CLAUDE_OPUS_4_6,
                local_cost_usd=_LOCAL_COST_USD,
                cloud_cost_usd=_COUNTERFACTUAL_USD,
                savings_usd=_COUNTERFACTUAL_USD,
                task_type=_TASK_TYPE,
                prompt_tokens=_PROMPT_TOKENS,
                completion_tokens=_COMPLETION_TOKENS,
            ),
            db,
        )
        row = db.query("savings_estimates")[0]
        assert row["task_type"] == _TASK_TYPE
        assert row["prompt_tokens"] == _PROMPT_TOKENS
        assert row["completion_tokens"] == _COMPLETION_TOKENS

    def test_negative_token_count_is_rejected(self) -> None:
        # A negative count is a write-path bug, not a value to clamp: silently
        # coercing it to 0 would hand the view the provenance claim "no tokens
        # served" and relabel a measured saving as an estimate.
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            ModelSavingsEstimatedEvent(
                event_timestamp=_EVENT_TIMESTAMP,
                session_id="sess-negative",
                model_local=MODEL_QWEN3_CODER_30B,
                model_cloud_baseline=MODEL_CLAUDE_OPUS_4_6,
                local_cost_usd=_LOCAL_COST_USD,
                cloud_cost_usd=_COUNTERFACTUAL_USD,
                savings_usd=_COUNTERFACTUAL_USD,
                prompt_tokens=-1,
            )


async def _apply(conn: asyncpg.Connection, migrations: tuple[Path, ...]) -> None:
    """Apply real migration files. Any failure raises -- nothing is skipped."""
    for migration in migrations:
        sql = migration.read_text(encoding="utf-8")
        sql = _CONCURRENT_INDEX.sub("CREATE INDEX", sql)
        # Production migrations target public explicitly; the test executes the
        # same SQL in a throwaway schema so it can prove the chain without
        # mutating other integration tests that share the database.
        sql = _PUBLIC_SCHEMA.sub("", sql)
        try:
            await conn.execute(sql)
        except Exception as exc:
            raise AssertionError(f"migration {migration.name} failed: {exc}") from exc


async def _isolated_schema(conn: asyncpg.Connection, name: str) -> None:
    """Build the real schema in a throwaway namespace.

    Every migration both views depend on is applied from disk -- no hand-written
    stand-in for delegation_events, which would duplicate a schema this node does
    not own and drift the moment node_projection_delegation changes.
    """
    await conn.execute(_ENSURE_DASHBOARD_ROLE)
    await conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
    await conn.execute(f'CREATE SCHEMA "{name}"')
    # public stays on the path so pgcrypto's gen_random_uuid() resolves.
    await conn.execute(f'SET search_path TO "{name}", public')


async def _insert_savings_row(conn: asyncpg.Connection, session_id: str) -> None:
    await conn.execute(
        _INSERT_SAVINGS_ROW,
        _EVENT_TIMESTAMP,
        session_id,
        _MODEL_NAME,
        MODEL_CLAUDE_OPUS_4_6,
        _LOCAL_COST_USD,
        _COUNTERFACTUAL_USD,
        _COUNTERFACTUAL_USD - _LOCAL_COST_USD,
    )


async def _single_session(conn: asyncpg.Connection) -> dict[str, object]:
    """The one element of ``projection_delegation_savings.sessions[]``."""
    row = await conn.fetchrow(
        "SELECT session FROM projection_delegation_savings, "
        "LATERAL jsonb_array_elements(sessions) AS session"
    )
    assert row is not None, "projection_delegation_savings produced no session rows"
    session: dict[str, object] = json.loads(row["session"])
    return session


@pytest.mark.integration
async def test_real_postgres_view_reproduces_then_corrects_the_matrix_row(
    postgres_fixture: asyncpg.Connection,
) -> None:
    """RED -> GREEN against a real Postgres, on one row.

    RED reproduces the ticket's live evidence exactly: task_type reads back as the
    model name and both token counts are 0. GREEN applies 082 + 083 and asserts the
    corrected projection over the *same* row, so the fix is proven to change the
    output rather than the fixture.
    """
    conn = postgres_fixture
    schema = "omn15533_proof"
    chain = _migration_chain()
    pre_fix = tuple(p for p in chain if p not in _FIX_MIGRATIONS)
    try:
        await _isolated_schema(conn, schema)
        await _apply(conn, pre_fix)
        await _insert_savings_row(conn, _CORRELATION_ID)

        # ---- RED: the shipped views, verbatim from the ticket's evidence -------
        red = await _single_session(conn)
        assert red["task_type"] == _MODEL_NAME
        assert red["task_type"] == red["model_name"]
        assert red["prompt_tokens"] == 0
        assert red["completion_tokens"] == 0
        assert red["savings_method"] == "measured"
        assert red["pricing_manifest_version"] == "savings-estimated"

        # ---- Apply the fix ----------------------------------------------------
        await _apply(conn, _FIX_MIGRATIONS)
        await conn.execute(
            """
            UPDATE savings_estimates
               SET task_type = $1, prompt_tokens = $2, completion_tokens = $3
             WHERE session_id = $4
            """,
            _TASK_TYPE,
            _PROMPT_TOKENS,
            _COMPLETION_TOKENS,
            _CORRELATION_ID,
        )

        # ---- GREEN ------------------------------------------------------------
        green = await _single_session(conn)
        # AC1: two distinct values.
        assert green["task_type"] == _TASK_TYPE
        assert green["model_name"] == _MODEL_NAME
        # AC2: the source terminal's real counts.
        assert green["prompt_tokens"] == _PROMPT_TOKENS
        assert green["completion_tokens"] == _COMPLETION_TOKENS
        # AC3: measured is now earned by served tokens.
        assert green["savings_method"] == "measured"
        assert green["usage_source"] == "measured"
        # AC4: the counterfactual is readable under a name that says so.
        assert green["counterfactual_baseline_usd"] == pytest.approx(
            float(_COUNTERFACTUAL_USD)
        )
        totals = await conn.fetchrow(
            "SELECT cumulative_cloud_cost_usd, cumulative_counterfactual_baseline_usd "
            "FROM projection_delegation_savings"
        )
        assert totals is not None
        assert totals["cumulative_counterfactual_baseline_usd"] == pytest.approx(
            totals["cumulative_cloud_cost_usd"]
        )
        # AC5: the series view is corrected too, and still aggregates the row.
        series = await conn.fetchrow(
            "SELECT task_count FROM projection_delegation_savings_series"
        )
        assert series is not None
        assert series["task_count"] == 1
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.mark.integration
async def test_real_postgres_unrecorded_tokens_are_never_labelled_measured(
    postgres_fixture: asyncpg.Connection,
) -> None:
    """AC3's falsifying conjunction must be unreachable, not merely unobserved.

    A row that predates migration 082 has NULL task_type and NULL tokens. It must
    render as an absent class with estimated/unknown provenance -- never as a model
    name, and never as a measured zero.
    """
    conn = postgres_fixture
    schema = "omn15533_legacy_row"
    try:
        await _isolated_schema(conn, schema)
        await _apply(conn, _migration_chain())
        # Written before 082: no task class, no counts, left untouched.
        await _insert_savings_row(conn, "sess-legacy")

        row = await _single_session(conn)
        assert row["task_type"] == ""
        assert row["task_type"] != row["model_name"]
        assert row["prompt_tokens"] is None
        assert row["completion_tokens"] is None
        assert row["savings_method"] == "estimated"
        assert row["usage_source"] == "unknown"
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.mark.integration
async def test_real_postgres_replace_view_preserves_column_contract(
    postgres_fixture: asyncpg.Connection,
) -> None:
    """CREATE OR REPLACE VIEW may only append.

    Applying the whole chain on a fresh schema is the real proof that 083 does not
    drop, rename, retype or reorder an existing output column: Postgres refuses the
    replacement outright if it does. The contract exposure must then match the
    view's actual columns, in order, so the projection API serves what it declares.
    """
    conn = postgres_fixture
    schema = "omn15533_column_contract"
    try:
        await _isolated_schema(conn, schema)
        await _apply(conn, _migration_chain())

        actual = [
            record["column_name"]
            for record in await conn.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = $1 AND table_name = 'projection_delegation_savings'
                ORDER BY ordinal_position
                """,
                schema,
            )
        ]
        contract = yaml.safe_load(_CONTRACT.read_text())
        declared = next(
            exposure["columns"]
            for exposure in contract["projection_api"]["exposures"]
            if exposure["table"] == "projection_delegation_savings"
        )
        assert actual == list(declared)
        assert actual[-1] == "cumulative_counterfactual_baseline_usd"
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
