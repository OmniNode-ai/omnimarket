# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18159 Phase 1b(ii): the delegation aggregate views carry their tenant.

WHY THIS EXISTS

The four delegation aggregate exposures are SQL views over ``delegation_events``
and, until this change, none of them had a ``tenant_id`` column. Each aggregated
the whole table and the WRITER supplied the tenant separately -- binding it as
the session scope so row-level security filtered the inputs, and selecting it as
a literal column so it could reach the compaction key and the message header.

That works for exactly one writer shape. The runtime kernel's read seam derives
its statement scope from ``filters["tenant_id"]``, which cannot be applied to a
column the view does not have. With no filter the tenant GUC is left unset,
FORCE row-level security hides every underlying row, and the aggregate publishes
ZEROS -- which on a dashboard reads as a quiet period rather than as a fault.
That is the same shape as the stale-row trap this ticket already fixed once:
plausible, wrong, and invisible.

Operator ruling 2026-09-11 (ledger RULING row): the views are re-grouped on
``tenant_id`` so the numbers are per-tenant for ANY reader, rather than correct
only for a reader who happened to arrive with the right session scope. A
caller-supplied scope on the read seam was refused on the same grounds it was
refused on the write side: a scope the runtime never verified is how
attribution-from-the-caller gets back in.

WHY TWO TENANTS, AND WHY A POSITIVE CONTROL

Every assertion here uses TWO tenants with deliberately different numbers. One
tenant cannot distinguish a correctly grouped view from the ungrouped one it
replaces -- both return the same single row. Two tenants with different counts
is the smallest fixture that can tell them apart.

The zero-row assertions carry positive controls for the same reason the ruling
demands one: an aggregate that reports zero is indistinguishable from an
aggregate that could not see its rows, and this whole phase exists because that
confusion is expensive.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)

#: Superseded by omnibase_infra's 0032 and fenced (OMN-15349); the disposable
#: schema performs the post-0032 conversion itself, as the OMN-16804 suite does.
_FENCED = "0031_delegation_events_tenant_id_to_uuid.sql"

AGGREGATE_VIEWS = (
    "projection_delegation_summary",
    "projection_delegation_model_routing",
    "projection_delegation_quality_gate",
    "projection_delegation_token_usage",
)

TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"


def _schema_safe(sql: str) -> str:
    """Strip CONCURRENTLY: it cannot run inside the implicit transaction.

    A property of the test driver, not of the migration -- a plain CREATE INDEX
    is schema-equivalent on a disposable single-connection schema.
    """
    return sql.replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX")


def _dsn() -> str:
    password = os.environ.get("INTEGRATION_POSTGRES_PASSWORD") or os.environ.get(
        "POSTGRES_PASSWORD"
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD/POSTGRES_PASSWORD unset: the "
            "per-tenant aggregate grain can only be shown against a real "
            "database"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5436")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnidash_analytics")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


@pytest.fixture
def views() -> Iterator[Any]:
    """A migrated disposable schema holding events for TWO tenants."""
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2.extras import RealDictCursor

    dsn = _dsn()
    schema = f"omn18159_agg_{uuid.uuid4().hex[:12]}"
    try:
        conn = psycopg2.connect(dsn)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres unreachable: {exc}")
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # The delegation migrations grant to two roles this node does not
            # create. app_dashboard and tenant_projection_writer are both
            # provisioned by OTHER migrations that a real lane applies before
            # these; a node-scoped fixture has neither, and the grant aborts
            # the migration. Provisioned with the same guarded CREATE ROLE the
            # owning migrations use, so the fixture matches the lane rather
            # than the migration being weakened to match the fixture.
            cur.execute(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles "
                "WHERE rolname = 'app_dashboard') THEN "
                "CREATE ROLE app_dashboard; END IF; "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles "
                "WHERE rolname = 'tenant_projection_writer') THEN "
                "CREATE ROLE tenant_projection_writer WITH NOLOGIN NOSUPERUSER "
                "NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION; END IF; "
                "END$$;"
            )
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(f"SET search_path TO {schema}, public")
            for path in sorted(_MIGRATIONS.glob("*.sql")):
                if path.name == _FENCED:
                    continue
                cur.execute(_schema_safe(path.read_text(encoding="utf-8")))
            # Two tenants, deliberately different counts: one tenant cannot
            # tell a grouped view from an ungrouped one.
            for tenant, rows in ((TENANT_A, 3), (TENANT_B, 1)):
                for index in range(rows):
                    cur.execute(
                        "INSERT INTO delegation_events "
                        "(correlation_id, tenant_id, task_type, delegated_to, "
                        " model_name, quality_gate_passed, timestamp, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())",
                        (
                            f"{tenant[:8]}-{index}",
                            tenant,
                            "code_review",
                            "local",
                            "qwen",
                            True,
                        ),
                    )

        class _Reader:
            def rows(
                self, view: str, tenant: str | None = None
            ) -> list[dict[str, Any]]:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(f"SET search_path TO {schema}, public")
                    if tenant is None:
                        cur.execute(f"SELECT * FROM {view}")
                    else:
                        cur.execute(
                            f"SELECT * FROM {view} WHERE tenant_id = %s", (tenant,)
                        )
                    return [dict(r) for r in cur.fetchall()]

        yield _Reader()
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()


@pytest.mark.parametrize("view", AGGREGATE_VIEWS)
class TestEveryAggregateViewCarriesItsTenant:
    def test_the_view_has_a_tenant_id_column(self, views: Any, view: str) -> None:
        rows = views.rows(view)
        assert rows, f"{view} returned no rows for a table holding two tenants"
        assert "tenant_id" in rows[0], sorted(rows[0])

    def test_it_returns_one_row_per_tenant(self, views: Any, view: str) -> None:
        """The grain the exposure's compaction key already declared.

        Before this change every one of these views returned exactly ONE row
        for the whole table, which is why a reader needed an out-of-band
        session scope to know whose numbers they were.
        """
        rows = views.rows(view)
        tenants = sorted(str(r["tenant_id"]) for r in rows)
        assert tenants == sorted([TENANT_A, TENANT_B]), tenants

    def test_a_tenant_filter_selects_exactly_that_tenant(
        self, views: Any, view: str
    ) -> None:
        """What makes the kernel's read seam able to scope this at all.

        The seam derives its statement scope from a ``tenant_id`` filter. A
        view without the column cannot take one.
        """
        rows = views.rows(view, TENANT_B)
        assert len(rows) == 1
        assert str(rows[0]["tenant_id"]) == TENANT_B


class TestTheNumbersAreThatTenantsNumbers:
    """The assertion one tenant could not make.

    With a single tenant, a grouped and an ungrouped view return the same
    values, so the fixture would pass against the very shape this replaces.
    """

    def test_summary_counts_are_per_tenant_not_whole_table(self, views: Any) -> None:
        rows = {
            str(r["tenant_id"]): r for r in views.rows("projection_delegation_summary")
        }
        assert rows[TENANT_A]["total_events"] == 3
        assert rows[TENANT_B]["total_events"] == 1
        # The ungrouped view would have reported 4 for whichever single row it
        # returned; asserting the SUM is not 4 on either side is the control.
        assert rows[TENANT_A]["total_events"] != 4
        assert rows[TENANT_B]["total_events"] != 4

    def test_model_routing_totals_are_per_tenant(self, views: Any) -> None:
        rows = {
            str(r["tenant_id"]): r
            for r in views.rows("projection_delegation_model_routing")
        }
        assert rows[TENANT_A]["total_delegations"] == 3
        assert rows[TENANT_B]["total_delegations"] == 1


class TestAbsenceIsProvenNotAssumed:
    """A zero needs a positive control, per the ruling and rule 16.

    An aggregate reporting zero and an aggregate that could not see its rows
    are indistinguishable from the outside, and this whole phase exists
    because that confusion is expensive.
    """

    def test_an_unknown_tenant_returns_no_row_and_a_known_one_does(
        self, views: Any
    ) -> None:
        absent = "33333333-3333-4333-8333-333333333333"
        assert views.rows("projection_delegation_summary", absent) == []
        # The positive control: the same query shape against a tenant that
        # does have rows must return one, or the empty result above proves
        # nothing about the tenant and only that the query was broken.
        control = views.rows("projection_delegation_summary", TENANT_A)
        assert len(control) == 1
        assert control[0]["total_events"] == 3


# --------------------------------------------------------------------------
# Invariant 6: no exposure loses its publisher, and now none lacks one
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestEveryBusBackedExposureHasAnInProcessPublisher:
    """The check that decides whether the runner can be retired at all.

    The plan's invariant 6 is that no exposure loses its publisher. Phase
    1b(i) gave the in-process path the per-row exposure; this phase gives it
    the four aggregates. Asserting the COUNT rather than naming topics is
    deliberate: a sixth exposure flagged ``bus_backed`` without a publish site
    must fail here rather than be discovered as an empty page, which is the
    ordering rule the handler's own constructor enforces.
    """

    def test_the_handler_covers_every_bus_backed_exposure(self) -> None:
        import yaml as _yaml

        from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
            HandlerProjectionDelegation,
        )
        from omnimarket.projection.discovery import (
            load_projection_exposures_from_contract,
        )

        contract_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_projection_delegation"
            / "contract.yaml"
        )
        contract = _yaml.safe_load(contract_path.read_text())
        exposures = load_projection_exposures_from_contract(
            contract, "projection_delegation", contract_path
        )
        bus_backed = [e.topic for e in exposures if e.bus_backed]

        handler = HandlerProjectionDelegation(publisher=None)
        served = {e.topic for e in handler._aggregate_exposures}
        if handler._row_exposure is not None:
            served.add(handler._row_exposure.topic)

        assert served == set(bus_backed), sorted(set(bus_backed) ^ served)
        assert len(bus_backed) == 5, bus_backed

    def test_an_unservable_bus_backed_shape_fails_construction(
        self, tmp_path: Path
    ) -> None:
        """A flag cannot outrun its writer.

        Flipping ``bus_backed`` on a shape nothing publishes turns an honest
        ``not_yet_bus_backed`` refusal into a confident empty page. Failing
        construction is what forces the publish call to land in the same
        change as the flag.
        """
        import yaml as _yaml

        from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
            HandlerProjectionDelegation,
        )

        src = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_projection_delegation"
            / "contract.yaml"
        )
        contract = _yaml.safe_load(src.read_text())
        exposures = contract["projection_api"]["exposures"]
        exposures.append(
            {
                "topic": "onex.snapshot.projection.delegation.invented.v1",
                "table": "delegation_events",
                "schema": "public",
                "columns": ["correlation_id", "session_id"],
                "bus_backed": True,
                # Neither the per-row key nor the aggregate key: no publisher.
                "key_columns": ["session_id"],
            }
        )
        forged = tmp_path / "contract.yaml"
        forged.write_text(_yaml.safe_dump(contract))

        with pytest.raises(RuntimeError, match="no publish site"):
            HandlerProjectionDelegation(contract_path=forged, publisher=None)
