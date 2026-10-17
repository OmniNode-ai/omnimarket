# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19988 mode 1: `onex dashboard` reports the native install when it starts.

The serve handler writes one native report through the environment store's
store-neutral write before it serves, and again on a short heartbeat while it
serves, so the row is never older than the freshness deadline while the
dashboard is up. The report carries the versions, the store, the access mode,
this start's token mint time and how to rotate it, which OMN-21015 AC10 reads.

Driven through HandlerLocalDashboardServe itself, with a captured serve, over a
real local SQLite store, as the other serve tests are.

Failure modes each test is written against:

* a start writes no native row, so a native install's panel is empty (AC3);
* the row names mode compose, or a native start writes compose rows;
* the versions are missing or are not the installed ones;
* token_minted_at is not this start's mint time, so a restart does not move it
  (the AK-2 falsifier);
* the token itself, or any part of the start URL, reaches the store;
* the access mode or the rotate guidance is missing;
* an install with no tenant identity gets a row under a default tenant,
  instead of no row;
* the row goes STALE while the dashboard is still serving, because nothing
  re-reports it, or the heartbeat moves last_change;
* a failing environment write stops the dashboard from serving.
* the dashboard node writes through another node's private handler package
  instead of the shared write seam.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from omnimarket.local_deployment.tenant_identity import (
    mint_local_tenant_identity,
    reset_local_tenant_identity_cache,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers import (
    handler_local_dashboard_serve as dashboard,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.models import (
    ModelLocalDashboardServeRequest,
)

pytestmark = pytest.mark.unit

TENANT = "6c24401e-0000-4000-8000-00000000000a"
T0 = datetime(2026, 10, 17, 4, tzinfo=UTC)


def _rows(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "local_environment" not in tables:
            return []
        return [dict(r) for r in conn.execute("SELECT * FROM local_environment")]
    finally:
        conn.close()


def _clock(start: datetime, step: timedelta) -> Iterator[datetime]:
    now = start
    while True:
        yield now
        now += step


def _start(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    pages: Path,
    *,
    tenant: str | None = TENANT,
    beats: int = 0,
    minted_at: datetime = T0,
    clock_start: datetime = T0,
) -> ModelLocalDashboardServeRequest:
    """One `onex dashboard` start that serves until ``beats`` heartbeats ran.

    The store exists, as `onex local init` leaves it when it mints the identity.
    """
    if not db_path.exists():
        mint_local_tenant_identity(db_path=db_path, tenant_uuid=UUID(TENANT))
        reset_local_tenant_identity_cache()
    monkeypatch.setattr(dashboard, "default_evidence_db_path", lambda: db_path)
    request = ModelLocalDashboardServeRequest(
        host="127.0.0.1", port=8123, tenant_id=tenant, token_minted_at=minted_at
    )
    ticks = _clock(clock_start, timedelta(seconds=15))
    beat_count = 0

    def clock() -> datetime:
        return next(ticks)

    def on_beat() -> None:
        nonlocal beat_count
        beat_count += 1

    async def serve(app: Any, host: str, port: int) -> None:
        for _ in range(500):
            if beat_count >= beats:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"only {beat_count} of {beats} heartbeats ran")

    asyncio.run(
        dashboard.HandlerLocalDashboardServe(
            serve=serve,
            pages=pages,
            heartbeat_seconds=0.01,
            clock=clock,
            on_environment_report=on_beat,
        ).handle(request)
    )
    return request


def test_a_start_writes_one_native_row_and_no_compose_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    _start(monkeypatch, db_path, tmp_path)
    rows = _rows(db_path)
    assert [(r["tenant_id"], r["mode"], r["service"]) for r in rows] == [
        (TENANT, "native", "local-runtime")
    ]
    row = rows[0]
    assert (row["observed_state"], row["verdict"], row["alert_code"]) == (
        "running",
        "running",
        None,
    )
    assert row["last_seen"] == T0.isoformat()


def test_the_row_carries_the_installed_versions_and_the_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    _start(monkeypatch, db_path, tmp_path)
    [row] = _rows(db_path)
    versions = json.loads(row["versions"])
    assert versions["omnimarket"] == version("omnimarket")
    assert versions["omnibase_core"] == version("omnibase_core")
    assert (row["store_kind"], row["store_location"]) == ("sqlite", str(db_path))


def test_the_row_carries_the_access_mode_mint_time_and_rotate_hint_never_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    request = _start(monkeypatch, db_path, tmp_path)
    [row] = _rows(db_path)
    assert row["access_mode"] == "loopback-token"
    assert row["token_minted_at"] == T0.isoformat()
    assert "restart" in row["rotate_hint"].lower()
    token = request.access_token.get_secret_value()
    with sqlite3.connect(db_path) as conn:
        dumped = "\n".join(conn.iterdump())
    assert token not in dumped
    assert token[:12] not in dumped


def test_a_restart_moves_the_mint_time_and_keeps_one_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    first = _start(monkeypatch, db_path, tmp_path)
    later = T0 + timedelta(minutes=5)
    second = _start(monkeypatch, db_path, tmp_path, minted_at=later, clock_start=later)
    assert (
        first.access_token.get_secret_value() != second.access_token.get_secret_value()
    )
    [row] = _rows(db_path)
    assert row["token_minted_at"] == later.isoformat()
    assert row["last_seen"] == later.isoformat()
    # The install kept running between the starts as far as the row knows.
    assert row["last_change"] == T0.isoformat()


def test_the_mint_time_defaults_to_the_moment_the_token_is_minted() -> None:
    before = datetime.now(UTC)
    request = ModelLocalDashboardServeRequest(host="127.0.0.1", port=8123)
    after = datetime.now(UTC)
    assert before <= request.token_minted_at <= after


def test_an_install_with_no_tenant_gets_no_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    _start(monkeypatch, db_path, tmp_path, tenant=None)
    assert _rows(db_path) == []
    assert "onex local init" in caplog.text


def test_a_start_with_no_store_yet_creates_none_and_writes_no_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(dashboard, "default_evidence_db_path", lambda: db_path)

    async def serve(app: Any, host: str, port: int) -> None:
        return None

    asyncio.run(
        dashboard.HandlerLocalDashboardServe(
            serve=serve, pages=tmp_path, heartbeat_seconds=0.01
        ).handle(
            ModelLocalDashboardServeRequest(
                host="127.0.0.1", port=8123, tenant_id=TENANT
            )
        )
    )
    assert not db_path.exists()


def test_the_heartbeat_keeps_the_row_fresh_without_moving_last_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    # The start report and five heartbeats, 15 s apart on the injected clock:
    # 75 s in, past the 60 s deadline, the row must still read RUNNING.
    _start(monkeypatch, db_path, tmp_path, beats=6)
    [row] = _rows(db_path)
    assert row["last_seen"] == (T0 + timedelta(seconds=75)).isoformat()
    assert row["last_change"] == T0.isoformat()
    assert row["verdict"] == "running"
    assert row["token_minted_at"] == T0.isoformat()


def test_a_failing_environment_write_does_not_stop_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    served: list[bool] = []

    def refuse(*_args: object, **_kwargs: object) -> bool:
        raise sqlite3.OperationalError("disk I/O error")

    mint_local_tenant_identity(db_path=db_path, tenant_uuid=UUID(TENANT))
    reset_local_tenant_identity_cache()
    monkeypatch.setattr(dashboard, "apply_local_environment_status", refuse)
    monkeypatch.setattr(dashboard, "default_evidence_db_path", lambda: db_path)

    async def serve(app: Any, host: str, port: int) -> None:
        served.append(True)

    request = ModelLocalDashboardServeRequest(
        host="127.0.0.1", port=8123, tenant_id=TENANT
    )
    asyncio.run(
        dashboard.HandlerLocalDashboardServe(
            serve=serve, pages=tmp_path, heartbeat_seconds=0.01
        ).handle(request)
    )
    assert served == [True]
    assert "disk I/O error" in caplog.text


def test_the_dashboard_writes_through_the_shared_seam_not_another_node() -> None:
    import omnimarket.nodes.node_local_dashboard_serve_effect as node

    imported: set[str] = set()
    for source in Path(node.__file__).parent.rglob("*.py"):
        for statement in ast.walk(ast.parse(source.read_text())):
            if isinstance(statement, ast.ImportFrom) and statement.module:
                imported.add(statement.module)
            elif isinstance(statement, ast.Import):
                imported.update(alias.name for alias in statement.names)
    assert "omnimarket.handlers.handler_local_environment_store" in imported
    assert not {
        name
        for name in imported
        if name.startswith("omnimarket.nodes.node_projection_local_environment")
    }
