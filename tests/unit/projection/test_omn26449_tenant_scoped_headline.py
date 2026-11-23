# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26449: the metering headline and onex metering's staleness check read only the tenant's own rows.

H1 another tenant's $0 unlisted run
H2 a run with no tenant (NULL tenant_id)
H3 a tenant named by slug through tenant_registry_mirror
H4 a tenant named by an upper-case UUID
H5 a slug nothing resolves reads no rows
H6 a store whose delegation_events has no tenant_id column reads every row (spec D1 default)
H8 a missing store still raises MeteringRecordsUnavailableError
S1 another tenant's newer run does not stale this tenant's row
S2 this tenant's own newer run still does
R1 refresh_metering_summary reads only the tenant's rows
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.cli.cli_metering import check_row_current
from omnimarket.models.model_projection_read import ModelProjectionReadRequest
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.sqlite_metering_reader import MeteringRecordsUnavailableError
from omnimarket.projection.sqlite_metering_summary import (
    read_summary_row,
    refresh_metering_after_terminal,
    refresh_metering_summary,
    resolve_metering_baseline_model,
)
from tests.helpers.tenant_registry import (
    PROJECTION_TENANT_SLUG,
    PROJECTION_TENANT_UUID,
    seed_tenant_registry,
)
from tests.unit.projection.test_omn24615_evaluated_runs_and_served_model import _ROUTING
from tests.unit.projection.test_omn24615_evaluated_runs_and_served_model import (
    _TOPICS as _ROUTING_TOPICS,
)
from tests.unit.projection.test_sqlite_delegation_savings_view_omn20008 import (
    _OTHER,
    _TENANT,
    _call,
    _delegation,
    _row,
    _store,
)

pytestmark = pytest.mark.unit

_TERMINAL = datetime(2026, 10, 11, tzinfo=UTC)
_LATER = "2026-10-12T00:00:00+00:00"
_UNLISTED = "vendor/unlisted-model"
_PRICED = "gpt-4o-mini"


def _run(
    cid: str, model: str | None, cost: float | None, **fields: Any
) -> dict[str, Any]:
    return _delegation(
        cid, model_name=model, delegated_to=model, cost_usd=cost, **fields
    )


def _measured(*runs: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return tuple(_call(str(r["correlation_id"]), "measured") for r in runs)


def _model() -> str:
    return resolve_metering_baseline_model().model


def _all_row(path: Path, tenant: str) -> Any:
    model = _model()
    refresh_metering_after_terminal(
        path, tenant_id=tenant, terminal_at=_TERMINAL, baseline_model=model
    )
    row = read_summary_row(path, tenant, "all", "", model)
    assert row is not None
    return row


def _routing_unmeasured(path: Path, tenant: str = _TENANT) -> int:
    handler = HandlerProjectionRead(
        topic_map=_ROUTING_TOPICS, row_source=SqliteTableRowSource(path)
    )
    result = asyncio.run(
        handler.handle(ModelProjectionReadRequest(topic=_ROUTING, tenant_id=tenant))
    )
    assert result.http_status == 200, result.response
    [routing] = (result.response or {})["rows"]
    return sum(
        int(m["unmeasured_cost_count"])
        for tier in routing["by_tier"]["tiers"]
        for m in tier["models"]
    )


def _counts(path: Path) -> tuple[int, int, int]:
    """(headline runs_unknown_spend, savings unpriced_call_count, routing unmeasured_cost_count)."""
    return (
        int(_all_row(path, _TENANT).runs_unknown_spend),
        int(_row(path)["unpriced_call_count"]),
        _routing_unmeasured(path),
    )


def _stale(path: Path, row: Any) -> Any:
    return check_row_current(
        path,
        row.model_dump(mode="json"),
        tenant_id=_TENANT,
        window_kind="all",
        window_start="",
        baseline_model=_model(),
    )


@pytest.mark.parametrize("owner", ["second_tenant", "null_tenant"])
def test_the_three_unpriced_counts_agree_beside_rows_that_are_not_the_tenants(
    tmp_path: Path, owner: str
) -> None:
    """H1/H2: another tenant's $0 unlisted run and a NULL-tenant run are excluded from all three counts."""
    priced = _run("priced", _PRICED, 0.0001)
    zero = _run(
        "zero", _UNLISTED, 0.0, tenant_id=_OTHER if owner == "second_tenant" else None
    )
    path = _store(tmp_path, (priced, zero), _measured(priced, zero))
    assert _counts(path) == (0, 0, 0)


def test_the_tenants_own_unpriced_run_still_counts_in_all_three(tmp_path: Path) -> None:
    """Control: the tenant's own $0 unlisted run counts in all three views."""
    priced = _run("priced", _PRICED, 0.0001)
    zero = _run("zero", _UNLISTED, 0.0)
    other = _run("other", _UNLISTED, 0.0, tenant_id=_OTHER)
    path = _store(tmp_path, (priced, zero, other), _measured(priced, zero, other))
    assert _counts(path) == (1, 1, 1)


def test_a_tenant_named_by_slug_reads_the_rows_recorded_under_its_uuid(
    tmp_path: Path,
) -> None:
    """H3: a tenant named by slug through tenant_registry_mirror reads its UUID-keyed rows."""
    uuid = str(PROJECTION_TENANT_UUID)
    runs = (
        _run("a", _PRICED, 0.0001, tenant_id=uuid),
        _run("b", _UNLISTED, 0.0, tenant_id=uuid),
        _run("c", _UNLISTED, 0.0, tenant_id=_OTHER),
    )
    path = _store(tmp_path, runs, _measured(*runs))
    seed_tenant_registry(SqliteDatabaseAdapter(path))
    row = _all_row(path, PROJECTION_TENANT_SLUG)
    assert (row.runs_total, row.runs_unknown_spend) == (2, 1)


def test_a_tenant_named_by_an_upper_case_uuid_reads_its_rows(tmp_path: Path) -> None:
    """H4: a tenant named by an upper-case UUID reads its rows."""
    runs = (
        _run("a", _PRICED, 0.0001),
        _run("b", _UNLISTED, 0.0),
        _run("c", _UNLISTED, 0.0, tenant_id=_OTHER),
    )
    path = _store(tmp_path, runs, _measured(*runs))
    row = _all_row(path, _TENANT.upper())
    assert (row.runs_total, row.runs_unknown_spend) == (2, 1)


def test_a_slug_nothing_resolves_reads_no_rows(tmp_path: Path) -> None:
    """H5: a slug nothing resolves reads no rows."""
    runs = (
        _run("a", _PRICED, 0.0001),
        _run("b", _UNLISTED, 0.0),
    )
    path = _store(tmp_path, runs, _measured(*runs))
    row = _all_row(path, "no-such-tenant-slug")
    assert row.runs_total == 0


def test_a_store_without_a_tenant_column_reads_every_row(tmp_path: Path) -> None:
    """H6: a store whose delegation_events has no tenant_id column reads every row (spec D1 default)."""
    runs = (
        _run("a", _PRICED, 0.0001),
        _run("b", _UNLISTED, 0.0),
    )
    path = _store(tmp_path, runs, _measured(*runs))
    conn = sqlite3.connect(path)
    try:
        # A store written before the tenant column also predates the views that read it.
        views = [
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
        ]
        for name in views:
            conn.execute(f'DROP VIEW "{name}"')
        conn.execute("ALTER TABLE delegation_events DROP COLUMN tenant_id")
        conn.commit()
    finally:
        conn.close()
    row = _all_row(path, _TENANT)
    assert (row.runs_total, row.runs_unknown_spend) == (2, 1)


def test_a_missing_store_still_refuses(tmp_path: Path) -> None:
    """H8: a missing store still raises MeteringRecordsUnavailableError."""
    with pytest.raises(MeteringRecordsUnavailableError):
        refresh_metering_after_terminal(
            tmp_path / "absent.sqlite",
            tenant_id=_TENANT,
            terminal_at=_TERMINAL,
            baseline_model=_model(),
        )


def test_another_tenants_newer_run_does_not_stale_this_tenants_row(
    tmp_path: Path,
) -> None:
    """S1: another tenant's newer run does not stale this tenant's row."""
    path = _store(
        tmp_path,
        (_run("priced", _PRICED, 0.0001),),
        _measured(_run("priced", _PRICED, 0.0001)),
    )
    row = _all_row(path, _TENANT)
    later = _run("other-later", _PRICED, 0.0001, tenant_id=_OTHER, created_at=_LATER)
    _store(tmp_path, (later,), _measured(later))
    refusal = _stale(path, row)
    assert refusal is None, refusal


def test_this_tenants_own_newer_run_still_stales_its_row(tmp_path: Path) -> None:
    """S2: this tenant's own newer run still stales its row."""
    path = _store(
        tmp_path,
        (_run("priced", _PRICED, 0.0001),),
        _measured(_run("priced", _PRICED, 0.0001)),
    )
    row = _all_row(path, _TENANT)
    later = _run("own-later", _PRICED, 0.0001, created_at=_LATER)
    _store(tmp_path, (later,), _measured(later))
    assert _stale(path, row) is not None


def test_refresh_metering_summary_reads_only_the_tenants_rows(tmp_path: Path) -> None:
    """R1: refresh_metering_summary reads only the tenant's rows."""
    runs = (
        _run("a", _PRICED, 0.0001),
        _run("b", _UNLISTED, 0.0, tenant_id=_OTHER),
    )
    path = _store(tmp_path, runs, _measured(*runs))
    rows = refresh_metering_summary(
        path, _TENANT, _model(), datetime(2026, 10, 13, tzinfo=UTC)
    )
    [all_row] = [r for r in rows if r.window_kind == "all"]
    assert (all_row.runs_total, all_row.runs_unknown_spend) == (1, 0)
