# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15919: every async projection write binds the tenant its own row carries,
and the adapter refuses one that does not.

MEASURED CAUSE (.201 dev lane, compose project ``omnibase-infra``, 2026-09-08,
with delegation migration 0026 released from the operator fence and applied --
``ROLLING_WORK_LEDGER.md`` row 4765).
``DelegationProjectionRunner._project_judge_verdict`` resolved ``tenant_id`` by
correlation join against ``delegation_events`` -- a ``uuid`` column since
delegation migration 0031 -- and then issued its INSERT through
``AsyncpgAdapter.execute`` with **no** ``tenant=`` argument. The adapter's
``_set_tenant_context`` therefore stamped ``app.tenant_id`` from
``resolve_read_tenant(None)``, whose table-less form is the house **slug**. Row
UUID versus GUC slug makes migration 0026's

    tenant_isolation  USING/WITH CHECK (tenant_id = current_setting('app.tenant_id', true))

false for every row, so under ``FORCE ROW LEVEL SECURITY`` every judge-verdict
write is refused. The sync twin (``PostgresSyncProjectionAdapter.upsert``)
derives the GUC from the row and was never affected.

This is the SAME class as OMN-17422 (the quality-gate terminal-site gap, ledger
~4660) and the same class OMN-15919 named originally: two independent resolvers
answering the two halves of one policy comparison, which agree only on a lane
where they happen to coincide.

WHAT THIS MODULE PINS, in three layers:

1. **The adapter refuses.** A statement that NAMES ``tenant_id`` and mutates
   rows, issued with no ``tenant=``, raises
   :class:`TenantScopedWriteUnboundError` before a connection is acquired. The
   adapter no longer supplies the missing half from a read-path resolver. This
   is what stops the class recurring at a site nobody has enumerated yet -- a
   new write either binds its tenant or fails loudly on its first execution.
2. **Every enumerated site binds it.** One test per async write site that names
   ``tenant_id``, asserting the ``tenant`` kwarg equals the value the row
   stores. The enumeration itself is pinned by
   :class:`TestNoAsyncWriteSiteNamesTenantIdWithoutBindingIt`, which re-derives
   the site list from the source tree rather than trusting this list to stay
   complete.
3. **A real policy accepts the fixed write.** The mock-DB assertions above
   accept any GUC/row pair without complaint -- exactly the blind spot that let
   this reach a deployed writer. The real-Postgres gate reuses the OMN-17422
   ``_rls_enforced_runner`` harness (a NOSUPERUSER / NOBYPASSRLS login role, all
   delegation migrations applied, so 0026's FORCE RLS actually binds) and proves
   a judge verdict lands its row.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import (
    AsyncpgAdapter,
    _is_tenant_scoped_write,
)
from omnimarket.events.delegation_judge_verdict import (
    ModelDelegationJudgeVerdictEvent,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_skill_executions.handlers.handler_skill_executions import (
    SkillExecutionsProjectionRunner,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection import (
    HandlerTenantCredentialsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_isolation import (
    HOUSE_TENANT_SLUG,
    HOUSE_TENANT_UUID,
    TenantScopedWriteUnboundError,
)
from tests.test_omn17422_quality_gate_tenant_scope_binding import (
    _BETA_TENANT_SLUG,
    _BETA_TENANT_UUID,
    _rls_enforced_runner,
    _seed_beta_delegation_row,
)

_HOUSE = {HOUSE_TENANT_SLUG, str(HOUSE_TENANT_UUID)}

# The real-DSN signal this repo's projection write-path gate requires, named
# here rather than only inside the reused harness: the FORCE-RLS assertions
# below are meaningless against a mock, so a run with no reachable database
# must SKIP loudly naming what is missing rather than pass silently.
_REAL_DSN_ENV = (
    "INTEGRATION_POSTGRES_HOST",
    "INTEGRATION_POSTGRES_PORT",
    "INTEGRATION_POSTGRES_USER",
    "INTEGRATION_POSTGRES_PASSWORD",
    "INTEGRATION_POSTGRES_DB",
)


def _require_real_postgres() -> None:
    """Skip (never pass, never error) when no real Postgres DSN is configured."""
    if os.environ.get("INTEGRATION_POSTGRES_PASSWORD") or os.environ.get(
        "POSTGRES_PASSWORD"
    ):
        return
    pytest.skip(
        "no real-Postgres DSN configured -- set "
        + ", ".join(_REAL_DSN_ENV)
        + " (or POSTGRES_PASSWORD) to run the OMN-15919 FORCE-RLS gate"
    )


def _mock_db() -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    db.fetchval = AsyncMock(return_value=None)
    return db


def _write_calls(db: AsyncMock, needle: str) -> list[Any]:
    return [c for c in db.execute.await_args_list if needle in str(c.args[0])]


def _judge_verdict_wire_record(
    *, correlation_id: str, tenant_id: str | None
) -> dict[str, Any]:
    """A ``delegation-judge-verdict.v1`` payload as the runner receives it.

    ``unwrap_envelope`` attaches the whole raw wire record under ``_envelope``
    before ``project_event`` sees it, so the envelope-side tenant stamp -- the
    only producer-recorded attribution available to a writer that cannot read
    across tenants under FORCE RLS -- is carried the same way here.
    """
    payload: dict[str, Any] = {
        "correlation_id": correlation_id,
        "task_type": "code-review",
        "score_source": "reproducible_judge",
        "judge_model": "glm-5.2",
        "judge_model_version": "2026-09-01",
        "judge_provider": "zhipu",
        "rubric_id": "delegation-code-review.v1",
        "rubric_hash": "sha256:" + "a" * 64,
        "prompt_hash": "sha256:" + "b" * 64,
        "input_hash": "sha256:" + "c" * 64,
        "temperature": 0.0,
        "judge_node_version": "0.38.22",
        "reasoning_hash": "sha256:" + "d" * 64,
        "verdict": "pass",
        "actual_score": 0.91,
        "failure_kind": None,
        "failure_message": None,
    }
    event = ModelDelegationJudgeVerdictEvent(**payload, event_hash="sha256:" + "e" * 64)
    payload["event_hash"] = event.compute_event_hash()
    unwrapped = dict(payload)
    unwrapped["_envelope"] = {
        "payload": payload,
        "envelope_id": str(uuid4()),
        "correlation_id": correlation_id,
        "event_type": "omnimarket.delegation-judge-verdict",
        "tenant_id": tenant_id,
    }
    return unwrapped


# ---------------------------------------------------------------------------
# 1. The adapter guard.
# ---------------------------------------------------------------------------


class TestTheAdapterRefusesAnUnboundTenantScopedWrite:
    """``AsyncpgAdapter`` no longer derives the GUC for a write that names
    ``tenant_id``. Deriving it is what made the two halves of the policy
    comparison answerable by two authorities."""

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO delegation_judge_verdict_events (event_hash, tenant_id) "
            "VALUES ($1, $2)",
            "UPDATE tenant_inference_routing_overlay SET secret_ref = NULL "
            "WHERE tenant_id = $1",
            "DELETE FROM delegation_events WHERE tenant_id = $1",
            "-- a leading comment must not disguise the statement\n"
            "INSERT INTO t (tenant_id) VALUES ($1)",
            "  \n\tinsert into t (TENANT_ID) values ($1)",
        ],
    )
    def test_a_write_naming_tenant_id_is_tenant_scoped(self, sql: str) -> None:
        assert _is_tenant_scoped_write(sql) is True

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT tenant_id FROM delegation_events WHERE correlation_id = $1",
            "SELECT 1 FROM delegation_events WHERE tenant_id = $1",
            "INSERT INTO schema_migrations (id) VALUES ($1)",
            "UPDATE node_registry SET last_seen = NOW() WHERE node_id = $1",
            "INSERT INTO t (requesting_tenant_identity) VALUES ($1)",
        ],
    )
    def test_reads_and_tenant_less_writes_are_left_alone(self, sql: str) -> None:
        """The guard must not fire on the many single-tenant and infrastructure
        relations this adapter also serves, nor on reads -- an unbound READ
        already fails closed at the policy (zero rows), and the read-path
        resolver remains the right answer for it."""
        assert _is_tenant_scoped_write(sql) is False

    @pytest.mark.asyncio
    async def test_execute_refuses_before_it_even_needs_a_pool(self) -> None:
        """Refusal happens before a connection is acquired, so a refused write
        issues no statement and leaves zero rows. Proven by refusing on an
        adapter that has no pool at all: an ``AssertionError`` here would mean
        the guard ran too late."""
        adapter = AsyncpgAdapter(dsn="postgresql://unused/unused")
        with pytest.raises(TenantScopedWriteUnboundError) as excinfo:
            await adapter.execute(
                "INSERT INTO delegation_judge_verdict_events "
                "(event_hash, tenant_id) VALUES ($1, $2)",
                "sha256:" + "0" * 64,
                str(HOUSE_TENANT_UUID),
            )
        assert "OMN-15919" in str(excinfo.value)
        assert "tenant=" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_execute_in_transaction_refuses_on_any_statement_in_the_batch(
        self,
    ) -> None:
        adapter = AsyncpgAdapter(dsn="postgresql://unused/unused")
        with pytest.raises(TenantScopedWriteUnboundError):
            await adapter.execute_in_transaction(
                [
                    ("INSERT INTO baselines (id) VALUES ($1)", ("a",)),
                    ("INSERT INTO t (tenant_id) VALUES ($1)", ("b",)),
                ]
            )

    @pytest.mark.asyncio
    async def test_execute_many_and_fetchval_are_guarded_too(self) -> None:
        adapter = AsyncpgAdapter(dsn="postgresql://unused/unused")
        with pytest.raises(TenantScopedWriteUnboundError):
            await adapter.execute_many(
                "INSERT INTO t (tenant_id) VALUES ($1)", [("a",)]
            )
        with pytest.raises(TenantScopedWriteUnboundError):
            await adapter.fetchval(
                "INSERT INTO t (tenant_id) VALUES ($1) RETURNING id", "a"
            )

    @pytest.mark.asyncio
    async def test_a_bound_write_is_not_refused(self) -> None:
        """The guard is about the MISSING half, not about the write. With
        ``tenant=`` supplied the statement passes the guard and fails only on
        the absent pool -- which is the assertion that follows it."""
        adapter = AsyncpgAdapter(dsn="postgresql://unused/unused")
        with pytest.raises(AssertionError):
            await adapter.execute(
                "INSERT INTO t (tenant_id) VALUES ($1)",
                str(HOUSE_TENANT_UUID),
                tenant=str(HOUSE_TENANT_UUID),
            )


# ---------------------------------------------------------------------------
# 2. Every enumerated site binds it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestJudgeVerdictBindsTheTenantItsRowCarries:
    async def test_the_insert_binds_the_joined_attribution(self) -> None:
        """The measured defect, as a property: whatever ``tenant_id`` the row
        stores is exactly what ``app.tenant_id`` is set to for the statement
        that stores it. RED before this change -- the INSERT carried no
        ``tenant=`` at all."""
        runner = DelegationProjectionRunner()
        db = _mock_db()
        correlation_id = str(uuid4())

        async def _execute(query: str, *_a: Any, **_k: Any) -> Any:
            if str(query).lstrip().startswith("SELECT tenant_id FROM"):
                return [{"tenant_id": _BETA_TENANT_UUID}]
            return []

        db.execute = AsyncMock(side_effect=_execute)
        runner._db = db  # type: ignore[assignment]

        assert await runner._project_judge_verdict(
            _judge_verdict_wire_record(
                correlation_id=correlation_id, tenant_id=_BETA_TENANT_SLUG
            )
        )

        writes = _write_calls(db, "INSERT INTO delegation_judge_verdict_events")
        assert writes, "expected the judge-verdict INSERT"
        call = writes[-1]
        assert call.kwargs["tenant"] == _BETA_TENANT_UUID
        # tenant_id is the last positional parameter ($19) of that statement.
        assert call.args[-1] == _BETA_TENANT_UUID

    async def test_the_attribution_probe_is_scoped_to_a_bound_tenant(self) -> None:
        """Under FORCE RLS a writer cannot discover a row's tenant by reading:
        with the GUC unset the predicate is NULL and the SELECT returns zero
        rows, indistinguishable from 'no such delegation'. The probe therefore
        runs under the producer-recorded envelope tenant. RED before this
        change -- the probe passed no ``tenant=`` and the adapter stamped the
        house slug from the read-path resolver."""
        runner = DelegationProjectionRunner()
        db = _mock_db()

        async def _execute(query: str, *_a: Any, **_k: Any) -> Any:
            if str(query).lstrip().startswith("SELECT tenant_id FROM"):
                return [{"tenant_id": _BETA_TENANT_UUID}]
            return []

        db.execute = AsyncMock(side_effect=_execute)
        runner._db = db  # type: ignore[assignment]

        await runner._project_judge_verdict(
            _judge_verdict_wire_record(
                correlation_id=str(uuid4()), tenant_id=_BETA_TENANT_SLUG
            )
        )

        probes = _write_calls(db, "SELECT tenant_id FROM delegation_events")
        assert probes, "expected the attribution probe"
        assert probes[-1].kwargs["tenant"] == _BETA_TENANT_UUID

    async def test_an_unattributed_verdict_probes_under_the_house_tenant(
        self,
    ) -> None:
        runner = DelegationProjectionRunner()
        db = _mock_db()

        async def _execute(query: str, *_a: Any, **_k: Any) -> Any:
            if str(query).lstrip().startswith("SELECT tenant_id FROM"):
                return [{"tenant_id": str(HOUSE_TENANT_UUID)}]
            return []

        db.execute = AsyncMock(side_effect=_execute)
        runner._db = db  # type: ignore[assignment]

        await runner._project_judge_verdict(
            _judge_verdict_wire_record(correlation_id=str(uuid4()), tenant_id=None)
        )

        probes = _write_calls(db, "SELECT tenant_id FROM delegation_events")
        assert probes[-1].kwargs["tenant"] in _HOUSE

    async def test_a_uuid_typed_attribution_is_not_discarded(self) -> None:
        """``delegation_events.tenant_id`` is ``uuid`` once delegation migration
        0031 has run, and asyncpg decodes that column as ``uuid.UUID`` -- not
        ``str``. The pre-fix ``isinstance(candidate, str)`` filter discarded
        every candidate on a converted lane, so EVERY verdict was routed to the
        DLQ for 'attribution unresolved' while the attribution sat in the result
        set. Both the .201 dev lane and this module's real-Postgres gate are
        converted lanes, so this is the shape that actually runs."""
        runner = DelegationProjectionRunner()
        db = _mock_db()

        async def _execute(query: str, *_a: Any, **_k: Any) -> Any:
            if str(query).lstrip().startswith("SELECT tenant_id FROM"):
                return [{"tenant_id": UUID(_BETA_TENANT_UUID)}]
            return []

        db.execute = AsyncMock(side_effect=_execute)
        runner._db = db  # type: ignore[assignment]

        assert await runner._project_judge_verdict(
            _judge_verdict_wire_record(
                correlation_id=str(uuid4()), tenant_id=_BETA_TENANT_SLUG
            )
        )
        writes = _write_calls(db, "INSERT INTO delegation_judge_verdict_events")
        assert writes, "a UUID-typed attribution must not be discarded"
        assert writes[-1].kwargs["tenant"] == _BETA_TENANT_UUID

    async def test_an_unresolvable_attribution_is_still_refused_to_the_dlq(
        self,
    ) -> None:
        """OMN-17627's refusal survives: unattributable stays loud and
        recoverable rather than being absorbed by the column default."""
        runner = DelegationProjectionRunner()
        db = _mock_db()
        runner._db = db  # type: ignore[assignment]
        runner._route_malformed_to_dlq = AsyncMock(return_value=False)  # type: ignore[method-assign]

        assert not await runner._project_judge_verdict(
            _judge_verdict_wire_record(correlation_id=str(uuid4()), tenant_id=None)
        )
        assert not _write_calls(db, "INSERT INTO delegation_judge_verdict_events")


@pytest.mark.asyncio
class TestGenerationEventsBindsTheTenantItStamps:
    async def test_the_insert_binds_the_stamped_house_tenant(self) -> None:
        runner = DelegationProjectionRunner()
        db = _mock_db()
        runner._db = db  # type: ignore[assignment]
        correlation_id = str(uuid4())

        await runner._project_generation_completed(
            {"correlation_id": correlation_id, "provider": "zhipu"},
            MessageMeta(partition=0, offset=1, fallback_id=correlation_id, topic="t"),
        )

        writes = _write_calls(db, "INSERT INTO generation_events")
        assert writes, "expected the generation_events INSERT"
        call = writes[-1]
        assert call.kwargs["tenant"] in _HOUSE
        assert call.args[-1] == call.kwargs["tenant"], (
            "the stamped row value and the bound GUC must be the same string -- "
            "one resolver, both halves of the policy comparison"
        )


@pytest.mark.asyncio
class TestSkillExecutionSnapshotsBindsTheTenantItStamps:
    async def test_the_upsert_binds_the_stamped_tenant(self) -> None:
        runner = SkillExecutionsProjectionRunner()
        db = _mock_db()
        runner._db = db  # type: ignore[assignment]

        await runner.project_event(
            runner.subscribe_topics[0],
            {"skill_name": "merge_sweep", "repo_id": "omnimarket"},
            MessageMeta(partition=0, offset=1, fallback_id="f", topic="t"),
        )

        writes = _write_calls(db, "INSERT INTO skill_execution_snapshots")
        assert writes, "expected the skill_execution_snapshots UPSERT"
        call = writes[-1]
        assert call.kwargs["tenant"] in _HOUSE
        assert call.args[-1] == call.kwargs["tenant"]


@pytest.mark.asyncio
class TestTenantCredentialsBindsTheTenantTheEventNames:
    """All four writes in this node name ``tenant_id`` -- two INSERTs on the
    register path, an INSERT and an UPDATE on the revoke path -- and this node
    is the one whose rows are BY DEFINITION not the house tenant's."""

    def _runner(self) -> tuple[HandlerTenantCredentialsProjectionRunner, AsyncMock]:
        runner = HandlerTenantCredentialsProjectionRunner()
        db = _mock_db()
        runner._db = db  # type: ignore[assignment]
        return runner, db

    async def test_register_binds_the_tenant_on_both_of_its_writes(self) -> None:
        runner, db = self._runner()
        await runner._project_registered(
            {
                "api_key_ref": "ref-1",
                "tenant_id": _BETA_TENANT_UUID,
                "provider": "openai",
                "name": "primary",
            },
            MessageMeta(partition=0, offset=1, fallback_id="f", topic="t"),
        )
        writes = [
            c
            for c in db.execute.await_args_list
            if str(c.args[0]).lstrip().startswith("INSERT INTO")
        ]
        assert writes, "expected the credential-registered writes"
        for call in writes:
            assert call.kwargs["tenant"] == _BETA_TENANT_UUID

    async def test_revoke_binds_the_tenant_on_both_of_its_writes(self) -> None:
        runner, db = self._runner()
        await runner._project_revoked(
            {"api_key_ref": "ref-1", "tenant_id": _BETA_TENANT_UUID},
            MessageMeta(partition=0, offset=1, fallback_id="f", topic="t"),
        )
        writes = [
            c
            for c in db.execute.await_args_list
            if str(c.args[0]).lstrip().startswith(("INSERT INTO", "UPDATE"))
        ]
        assert len(writes) >= 2, "expected the tombstone INSERT and the overlay UPDATE"
        for call in writes:
            assert call.kwargs["tenant"] == _BETA_TENANT_UUID


class TestNoAsyncWriteSiteNamesTenantIdWithoutBindingIt:
    """The enumeration, re-derived rather than trusted.

    The six sites fixed by OMN-15919 are only the ones that existed when it was
    written. This scan is what keeps the list from silently going stale: any new
    ``execute``/``execute_many``/``execute_in_transaction``/``fetchval`` call
    whose literal SQL mutates rows and names ``tenant_id`` must pass ``tenant=``.

    It is a static complement to the adapter guard, not a substitute: the guard
    catches dynamically-built SQL this scan cannot read, and this scan catches a
    site whose code path no test exercises."""

    _METHODS = {"execute", "execute_many", "execute_in_transaction", "fetchval"}

    @staticmethod
    def _literal(node: ast.AST) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(
                TestNoAsyncWriteSiteNamesTenantIdWithoutBindingIt._literal(v)
                for v in node.values
            )
        if isinstance(node, ast.FormattedValue):
            return "{}"
        return ""

    def test_no_unbound_tenant_scoped_write_site_remains(self) -> None:
        src_root = pathlib.Path(__file__).resolve().parents[1] / "src" / "omnimarket"
        offenders: list[str] = []
        for path in sorted(src_root.rglob("*.py")):
            if "/tests/" in str(path):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (
                    not isinstance(func, ast.Attribute)
                    or func.attr not in self._METHODS
                ):
                    continue
                sql = " ".join(self._literal(a) for a in node.args)
                if not sql.strip():
                    continue
                if not re.search(
                    r"^\s*(INSERT\s+INTO|UPDATE\b|DELETE\s+FROM)", sql, re.IGNORECASE
                ):
                    continue
                if not re.search(r"\btenant_id\b", sql, re.IGNORECASE):
                    continue
                if any(k.arg == "tenant" for k in node.keywords):
                    continue
                offenders.append(f"{path}:{node.lineno}")
        assert not offenders, (
            "these async write sites name tenant_id but bind no tenant, so the "
            "adapter would have to derive the GUC from a resolver that has never "
            "seen the row (OMN-15919): " + ", ".join(offenders)
        )


# ---------------------------------------------------------------------------
# 3. A real policy accepts the fixed write.
#
# Reuses the OMN-17422 harness: a NOSUPERUSER / NOBYPASSRLS login role in a
# throwaway schema with every delegation migration applied, so migration 0026's
# FORCE ROW LEVEL SECURITY on delegation_judge_verdict_events actually binds.
# SKIPS (never ERRORs) without a reachable database, per the module idiom.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestRealPostgresForceRlsAcceptsTheBoundJudgeVerdict:
    async def test_red_the_house_guc_is_refused_against_a_beta_tenant_verdict(
        self,
    ) -> None:
        """The pre-fix shape, proven by execution: the verdict row carries the
        delegation's real tenant while ``app.tenant_id`` holds the house value
        the read-path resolver returns. This is the .201 refusal verbatim."""
        _require_real_postgres()
        async with _rls_enforced_runner() as (runner, admin, _role):
            correlation_id = str(uuid4())
            await _seed_beta_delegation_row(admin, correlation_id)

            with pytest.raises(asyncpg.PostgresError) as excinfo:
                await runner.db.execute(
                    "INSERT INTO delegation_judge_verdict_events "
                    "(event_hash, correlation_id, tenant_id) VALUES ($1, $2, $3)",
                    "sha256:" + "f" * 64,
                    correlation_id,
                    _BETA_TENANT_UUID,
                    tenant=str(HOUSE_TENANT_UUID),
                )
            assert "row-level security policy" in str(excinfo.value)

    async def test_green_a_judge_verdict_lands_its_row_under_force_rls(self) -> None:
        """The fixed path end to end: the probe finds the delegation under the
        envelope-recorded tenant, the INSERT binds that same tenant, and the
        policy accepts the row. An absent row here is the .201 failure."""
        _require_real_postgres()
        async with _rls_enforced_runner() as (runner, admin, _role):
            correlation_id = str(uuid4())
            await _seed_beta_delegation_row(admin, correlation_id)

            projected = await runner._project_judge_verdict(
                _judge_verdict_wire_record(
                    correlation_id=correlation_id, tenant_id=_BETA_TENANT_SLUG
                )
            )
            assert projected is True

            async with admin.transaction():
                await admin.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", _BETA_TENANT_UUID
                )
                row = await admin.fetchrow(
                    "SELECT tenant_id, verdict, actual_score FROM "
                    "delegation_judge_verdict_events WHERE correlation_id = $1",
                    correlation_id,
                )
            assert row is not None, (
                "the judge verdict must reach the projection plane -- an absent "
                "row is the FORCE-RLS refusal this ticket closes"
            )
            assert str(row["tenant_id"]) == _BETA_TENANT_UUID
            assert row["verdict"] == "pass"
