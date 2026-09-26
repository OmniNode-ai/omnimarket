# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for omnimarket.routing.dod_overlay (ticket OMN-19528)."""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.routing.dod_overlay import (
    ModelDodModelSignal,
    ModelRoutingDodOverlay,
    build_dod_overlay,
)

TENANT_ID = "820272f9-4aaf-5add-a2df-0af942852ab2"
TASK_TYPE = "document"
BASE_TIME = datetime(2026, 9, 25, 3, 0, tzinfo=UTC)


def _row(
    task_type: str = TASK_TYPE,
    correlation_id: str | None = None,
    tier_name: str = "local",
    model_name: str = "Qwen3.8-27B",
    created_at: datetime = BASE_TIME,
    verdict_correlation_id: str | None = None,
    verdict_status: str = "verified",
    verdict_outcome: str = "done",
    verdict_completed_at: datetime = BASE_TIME,
) -> dict[str, Any]:
    """Build a single delegation/verdict row with sensible defaults."""
    return {
        "task_type": task_type,
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "tier_name": tier_name,
        "model_name": model_name,
        "created_at": created_at,
        "verdict_correlation_id": verdict_correlation_id or str(uuid.uuid4()),
        "verdict_status": verdict_status,
        "verdict_outcome": verdict_outcome,
        "verdict_completed_at": verdict_completed_at,
    }


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the two env vars that could override lookback/window."""
    monkeypatch.delenv("DELEGATION_ROI_LOOKBACK_ROWS", raising=False)
    monkeypatch.delenv("DELEGATION_ROI_WINDOW_SECONDS", raising=False)


@pytest.mark.unit
def test_only_matching_task_type_counts() -> None:
    """Rule 1: rows with a different task_type are excluded."""
    rows = [
        _row(task_type=TASK_TYPE, verdict_outcome="done"),
        _row(task_type="invoice", verdict_outcome="done"),
        _row(task_type="invoice", verdict_outcome="done"),
    ]
    overlay = build_dod_overlay(
        rows,
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        min_samples=1,
        success_floor=0.5,
    )
    assert overlay.task_type == TASK_TYPE
    assert overlay.tenant_id == TENANT_ID
    assert len(overlay.roi_overlay.signals) == 1
    sig = overlay.roi_overlay.signals[0]
    assert sig.sample_count == 1
    assert sig.success_count == 1
    assert sig.success_rate == 1.0


@pytest.mark.unit
def test_only_verified_and_failed_count_as_samples() -> None:
    """Rule 2: skipped/unresolved/pending rows are ignored entirely."""
    rows = [
        _row(verdict_status="verified", verdict_outcome="done"),
        _row(verdict_status="failed", verdict_outcome="refused"),
        _row(verdict_status="skipped", verdict_outcome="done"),
        _row(verdict_status="unresolved", verdict_outcome="done"),
        _row(verdict_status="pending", verdict_outcome="done"),
    ]
    overlay = build_dod_overlay(
        rows,
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        min_samples=2,
        success_floor=0.5,
    )
    sig = overlay.roi_overlay.signals[0]
    assert sig.sample_count == 2
    assert sig.success_count == 1
    assert sig.success_rate == 0.5


@pytest.mark.unit
def test_pass_requires_done_outcome() -> None:
    """Rule 3: verified+refused is a fail; only done counts as pass."""
    rows = [
        _row(verdict_status="verified", verdict_outcome="done"),
        _row(verdict_status="verified", verdict_outcome="refused"),
        _row(verdict_status="failed", verdict_outcome="done"),
        _row(verdict_status="failed", verdict_outcome="refused"),
    ]
    overlay = build_dod_overlay(
        rows,
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        min_samples=4,
        success_floor=0.5,
    )
    sig = overlay.roi_overlay.signals[0]
    assert sig.sample_count == 4
    assert sig.success_count == 2
    assert sig.success_rate == 0.5


@pytest.mark.unit
def test_latest_verdict_per_correlation_wins() -> None:
    """Rule 4: per correlation_id, only the latest verdict_completed_at counts;
    ties broken by larger verdict_correlation_id string."""
    cid = str(uuid.uuid4())
    rows = [
        _row(
            correlation_id=cid,
            verdict_correlation_id="aaa",
            verdict_status="verified",
            verdict_outcome="done",
            verdict_completed_at=BASE_TIME,
        ),
        _row(
            correlation_id=cid,
            verdict_correlation_id="bbb",
            verdict_status="verified",
            verdict_outcome="refused",
            verdict_completed_at=BASE_TIME,
        ),
        _row(
            correlation_id=cid,
            verdict_correlation_id="ccc",
            verdict_status="failed",
            verdict_outcome="refused",
            verdict_completed_at=BASE_TIME,
        ),
    ]
    # All three share the same completed_at; "ccc" is the largest string,
    # and it is a fail.
    overlay = build_dod_overlay(
        rows,
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        min_samples=1,
        success_floor=0.5,
    )
    sig = overlay.roi_overlay.signals[0]
    assert sig.sample_count == 1
    assert sig.success_count == 0
    assert sig.success_rate == 0.0


@pytest.mark.unit
def test_empty_tier_or_model_name_ignored() -> None:
    """Rule 5: rows with empty tier_name or model_name are ignored."""
    rows = [
        _row(tier_name="", model_name="Qwen3.8-27B", verdict_outcome="done"),
        _row(tier_name="local", model_name="", verdict_outcome="done"),
        _row(tier_name="local", model_name="Qwen3.8-27B", verdict_outcome="done"),
    ]
    overlay = build_dod_overlay(
        rows,
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        min_samples=1,
        success_floor=0.5,
    )
    assert len(overlay.roi_overlay.signals) == 1
    sig = overlay.roi_overlay.signals[0]
    assert sig.tier_name == "local"
    assert sig.sample_count == 1
    assert len(overlay.model_signals) == 1
    assert overlay.model_signals[0].tier_name == "local"
    assert overlay.model_signals[0].model_name == "Qwen3.8-27B"
    assert overlay.model_signals[0].sample_count == 1


@pytest.mark.unit
def test_model_signals_and_tier_pooling() -> None:
    """Rule 6: model_signals has one entry per (tier, model); tier signal pools
    every model in that tier."""
    rows = [
        _row(tier_name="local", model_name="Qwen3.8-27B", verdict_outcome="done"),
        _row(tier_name="local", model_name="Qwen3.8-27B", verdict_outcome="refused"),
        _row(tier_name="local", model_name="glm-5.3-flash", verdict_outcome="done"),
        _row(
            tier_name="cheap_cloud",
            model_name="gemini-2.5-flash",
            verdict_outcome="done",
        ),
    ]
    overlay = build_dod_overlay(
        rows,
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        min_samples=1,
        success_floor=0.5,
    )
    # model_signals sorted by (tier_name, model_name)
    assert len(overlay.model_signals) == 3
    assert all(isinstance(s, ModelDodModelSignal) for s in overlay.model_signals)
    keys = [(s.tier_name, s.model_name) for s in overlay.model_signals]
    assert keys == [
        ("cheap_cloud", "gemini-2.5-flash"),
        ("local", "Qwen3.8-27B"),
        ("local", "glm-5.3-flash"),
    ]
    cheap = overlay.model_signals[0]
    assert cheap.sample_count == 1
    assert cheap.pass_count == 1
    assert cheap.pass_rate == 1.0
    qwen = overlay.model_signals[1]
    assert qwen.sample_count == 2
    assert qwen.pass_count == 1
    assert qwen.pass_rate == 0.5
    glm = overlay.model_signals[2]
    assert glm.sample_count == 1
    assert glm.pass_count == 1
    assert glm.pass_rate == 1.0

    # Tier signals: local pools both models (3 samples, 2 passes), cheap_cloud
    # has 1 sample, 1 pass.
    assert len(overlay.roi_overlay.signals) == 2
    tier_keys = [s.tier_name for s in overlay.roi_overlay.signals]
    assert "local" in tier_keys
    assert "cheap_cloud" in tier_keys
    local_sig = next(s for s in overlay.roi_overlay.signals if s.tier_name == "local")
    assert local_sig.sample_count == 3
    assert local_sig.success_count == 2
    assert local_sig.success_rate == pytest.approx(2 / 3)
    cloud_sig = next(
        s for s in overlay.roi_overlay.signals if s.tier_name == "cheap_cloud"
    )
    assert cloud_sig.sample_count == 1
    assert cloud_sig.success_count == 1
    assert cloud_sig.success_rate == 1.0


@pytest.mark.unit
def test_suppression_rules() -> None:
    """Rule 7: suppressed only when sample_count >= min_samples AND
    rate < success_floor."""
    # Case A: 5 samples, 1 pass, min_samples=5, floor=0.5 -> suppressed
    rows_a = [
        _row(verdict_outcome="done"),
        _row(verdict_outcome="refused"),
        _row(verdict_outcome="refused"),
        _row(verdict_outcome="refused"),
        _row(verdict_outcome="refused"),
    ]
    overlay_a = build_dod_overlay(
        rows_a,
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        min_samples=5,
        success_floor=0.5,
    )
    sig_a = overlay_a.roi_overlay.signals[0]
    assert sig_a.sample_count == 5
    assert sig_a.success_count == 1
    assert sig_a.success_rate == 0.2
    assert sig_a.suppressed is True
    assert "local" in overlay_a.roi_overlay.suppressed_tiers

    # Case B: 4 samples, 0 passes, min_samples=5 -> not suppressed
    rows_b = [
        _row(verdict_outcome="refused"),
        _row(verdict_outcome="refused"),
        _row(verdict_outcome="refused"),
        _row(verdict_outcome="refused"),
    ]
    overlay_b = build_dod_overlay(
        rows_b,
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        min_samples=5,
        success_floor=0.5,
    )
    sig_b = overlay_b.roi_overlay.signals[0]
    assert sig_b.sample_count == 4
    assert sig_b.success_count == 0
    assert sig_b.suppressed is False
    assert "local" not in overlay_b.roi_overlay.suppressed_tiers

    # Case C: 6 samples, 3 passes, floor=0.5 -> not suppressed (0.5 is not
    # below 0.5)
    rows_c = [
        _row(verdict_outcome="done"),
        _row(verdict_outcome="done"),
        _row(verdict_outcome="done"),
        _row(verdict_outcome="refused"),
        _row(verdict_outcome="refused"),
        _row(verdict_outcome="refused"),
    ]
    overlay_c = build_dod_overlay(
        rows_c,
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        min_samples=5,
        success_floor=0.5,
    )
    sig_c = overlay_c.roi_overlay.signals[0]
    assert sig_c.sample_count == 6
    assert sig_c.success_count == 3
    assert sig_c.success_rate == 0.5
    assert sig_c.suppressed is False
    assert "local" not in overlay_c.roi_overlay.suppressed_tiers


@pytest.mark.unit
def test_lookback_rows_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 8: lookback_rows=3 keeps only the 3 most recent rows per tier by
    created_at. 5 old passes then 3 recent fails -> suppressed with lookback
    3, not suppressed without a window."""
    _clear_env(monkeypatch)
    old_time = BASE_TIME
    recent_time = BASE_TIME + timedelta(hours=1)
    rows = [
        _row(
            created_at=old_time,
            verdict_completed_at=old_time,
            verdict_outcome="done",
        ),
        _row(
            created_at=old_time,
            verdict_completed_at=old_time,
            verdict_outcome="done",
        ),
        _row(
            created_at=old_time,
            verdict_completed_at=old_time,
            verdict_outcome="done",
        ),
        _row(
            created_at=old_time,
            verdict_completed_at=old_time,
            verdict_outcome="done",
        ),
        _row(
            created_at=old_time,
            verdict_completed_at=old_time,
            verdict_outcome="done",
        ),
        _row(
            created_at=recent_time,
            verdict_completed_at=recent_time,
            verdict_outcome="refused",
        ),
        _row(
            created_at=recent_time,
            verdict_completed_at=recent_time,
            verdict_outcome="refused",
        ),
        _row(
            created_at=recent_time,
            verdict_completed_at=recent_time,
            verdict_outcome="refused",
        ),
    ]

    # With lookback_rows=3: only the 3 recent fails count -> 0/3, suppressed.
    overlay_with = build_dod_overlay(
        rows,
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        min_samples=3,
        success_floor=0.5,
        lookback_rows=3,
        window_seconds=None,
    )
    sig_with = overlay_with.roi_overlay.signals[0]
    assert sig_with.sample_count == 3
    assert sig_with.success_count == 0
    assert sig_with.success_rate == 0.0
    assert sig_with.suppressed is True
    assert "local" in overlay_with.roi_overlay.suppressed_tiers

    # Without a window: all 8 rows count -> 5/8 = 0.625 >= 0.5, not
    # suppressed.
    overlay_without = build_dod_overlay(
        rows,
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        min_samples=3,
        success_floor=0.5,
        lookback_rows=None,
        window_seconds=None,
    )
    sig_without = overlay_without.roi_overlay.signals[0]
    assert sig_without.sample_count == 8
    assert sig_without.success_count == 5
    assert sig_without.success_rate == pytest.approx(5 / 8)
    assert sig_without.suppressed is False
    assert "local" not in overlay_without.roi_overlay.suppressed_tiers


@pytest.mark.unit
def test_window_seconds_drops_old_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 9: window_seconds=3600 with now fixed drops rows older than one
    hour."""
    _clear_env(monkeypatch)
    now = BASE_TIME
    old_time = now - timedelta(hours=2)
    recent_time = now - timedelta(minutes=30)
    rows = [
        _row(
            created_at=old_time,
            verdict_completed_at=old_time,
            verdict_outcome="done",
        ),
        _row(
            created_at=recent_time,
            verdict_completed_at=recent_time,
            verdict_outcome="done",
        ),
    ]
    overlay = build_dod_overlay(
        rows,
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        min_samples=1,
        success_floor=0.5,
        lookback_rows=None,
        window_seconds=3600,
        now=now,
    )
    sig = overlay.roi_overlay.signals[0]
    assert sig.sample_count == 1
    assert sig.success_count == 1
    assert sig.success_rate == 1.0


@pytest.mark.unit
def test_deterministic_under_shuffle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 10: shuffling the input rows gives an equal overlay."""
    _clear_env(monkeypatch)
    rows = [
        _row(tier_name="local", model_name="Qwen3.8-27B", verdict_outcome="done"),
        _row(tier_name="local", model_name="glm-5.3-flash", verdict_outcome="refused"),
        _row(
            tier_name="cheap_cloud",
            model_name="gemini-2.5-flash",
            verdict_outcome="done",
        ),
        _row(tier_name="local", model_name="Qwen3.8-27B", verdict_outcome="refused"),
        _row(
            tier_name="cheap_cloud",
            model_name="gemini-2.5-flash",
            verdict_outcome="refused",
        ),
    ]
    original = build_dod_overlay(
        rows,
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        min_samples=2,
        success_floor=0.5,
        lookback_rows=None,
        window_seconds=None,
    )

    rng = random.Random(42)
    shuffled = rows.copy()
    rng.shuffle(shuffled)
    reshuffled = build_dod_overlay(
        shuffled,
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        min_samples=2,
        success_floor=0.5,
        lookback_rows=None,
        window_seconds=None,
    )

    assert original == reshuffled


@pytest.mark.unit
def test_empty_input() -> None:
    """Rule 11: empty input gives no tier signals, no model signals, and
    nothing suppressed."""
    overlay = build_dod_overlay(
        [],
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        min_samples=5,
        success_floor=0.5,
    )
    assert isinstance(overlay, ModelRoutingDodOverlay)
    assert overlay.task_type == TASK_TYPE
    assert overlay.tenant_id == TENANT_ID
    assert overlay.roi_overlay.signals == ()
    assert overlay.roi_overlay.suppressed_tiers == frozenset()
    assert overlay.model_signals == ()


# --- The I/O boundary: reader, tenancy, fail-open, port wiring (AC2, AC4) ---------


DELEGATION_DSN = "postgresql://reader@db/omnidash_analytics"
VERDICT_DSN = "postgresql://internal@db/omnidash_analytics"


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self._conn.statements.append((sql, params, self._conn.autocommit))
        if self._conn.fail_on_select and "SELECT set_config" not in sql:
            raise RuntimeError("relation does not exist")

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._conn.rows)


class _FakeConn:
    def __init__(
        self, rows: list[dict[str, Any]] | None = None, *, fail_on_select: bool = False
    ) -> None:
        self.rows = rows or []
        self.fail_on_select = fail_on_select
        self.statements: list[tuple[str, object, bool]] = []
        self.autocommit = True
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, cursor_factory: object = None) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _patch_connect(
    monkeypatch: pytest.MonkeyPatch, conns: dict[str, _FakeConn]
) -> None:
    from omnimarket.projection import postgres_read_database

    def _connect(dsn: str, *, connect_timeout: int = 3) -> _FakeConn:
        return conns[dsn]

    monkeypatch.setattr(postgres_read_database, "connect_read_only", _connect)


@pytest.mark.unit
def test_pure_join_matches_text_run_ids_to_uuid_verdicts() -> None:
    from omnimarket.routing.dod_overlay import join_delegations_to_verdicts

    run = str(uuid.uuid4())
    other = str(uuid.uuid4())
    delegations = [
        {
            "task_type": TASK_TYPE,
            "correlation_id": run.upper(),
            "tier_name": "local",
            "model_name": "Qwen3.8-27B",
            "created_at": BASE_TIME,
        },
        {
            "task_type": TASK_TYPE,
            "correlation_id": other,
            "tier_name": "local",
            "model_name": "Qwen3.8-27B",
            "created_at": BASE_TIME,
        },
    ]
    verdicts = [
        {
            "verdict_correlation_id": "v1",
            "delegation_correlation_id": run,
            "verdict_status": "verified",
            "verdict_outcome": "done",
            "verdict_completed_at": BASE_TIME,
        },
        {
            "verdict_correlation_id": "v2",
            "delegation_correlation_id": None,
            "verdict_status": "verified",
            "verdict_outcome": "done",
            "verdict_completed_at": BASE_TIME,
        },
    ]

    joined = join_delegations_to_verdicts(delegations, verdicts)

    assert len(joined) == 1
    assert joined[0]["correlation_id"] == run
    assert joined[0]["verdict_correlation_id"] == "v1"
    assert joined[0]["model_name"] == "Qwen3.8-27B"


@pytest.mark.unit
def test_reader_reads_verdicts_then_the_tenant_scoped_delegations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.routing.dod_overlay import PostgresDodOutcomeReader

    run = str(uuid.uuid4())
    verdict_conn = _FakeConn(
        rows=[
            {
                "verdict_correlation_id": "v1",
                "delegation_correlation_id": run,
                "verdict_status": "verified",
                "verdict_outcome": "done",
                "verdict_completed_at": BASE_TIME,
            }
        ]
    )
    delegation_conn = _FakeConn(
        rows=[
            {
                "task_type": TASK_TYPE,
                "correlation_id": run,
                "tier_name": "local",
                "model_name": "Qwen3.8-27B",
                "created_at": BASE_TIME,
            }
        ]
    )
    _patch_connect(
        monkeypatch, {VERDICT_DSN: verdict_conn, DELEGATION_DSN: delegation_conn}
    )
    reader = PostgresDodOutcomeReader(DELEGATION_DSN, VERDICT_DSN)

    rows = reader.read_dod_outcomes(task_type=TASK_TYPE, tenant_id=TENANT_ID)

    assert [r["verdict_outcome"] for r in rows] == ["done"]
    (verdict_sql, _, _) = verdict_conn.statements[0]
    assert "omninode_internal.dod_verify_runs" in verdict_sql
    assert "delegation_correlation_id IS NOT NULL" in verdict_sql
    guc_sql, guc_params, guc_autocommit = delegation_conn.statements[0]
    assert "set_config" in guc_sql
    assert guc_params == ("app.tenant_id", TENANT_ID)
    assert guc_autocommit is False  # inside the transaction, not a lost GUC
    read_sql, read_params, _ = delegation_conn.statements[1]
    assert "public.delegation_events" in read_sql
    assert "tenant_id = %(tenant_id)s::uuid" in read_sql
    assert read_params == {
        "task_type": TASK_TYPE,
        "tenant_id": TENANT_ID,
        "run_ids": [run],
    }
    assert delegation_conn.commits == 1
    assert delegation_conn.autocommit is True


@pytest.mark.unit
def test_reader_with_no_linked_verdict_never_reads_delegations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.routing.dod_overlay import PostgresDodOutcomeReader

    verdict_conn = _FakeConn(rows=[])
    delegation_conn = _FakeConn(rows=[])
    _patch_connect(
        monkeypatch, {VERDICT_DSN: verdict_conn, DELEGATION_DSN: delegation_conn}
    )
    reader = PostgresDodOutcomeReader(DELEGATION_DSN, VERDICT_DSN)

    assert reader.read_dod_outcomes(task_type=TASK_TYPE, tenant_id=TENANT_ID) == []
    assert delegation_conn.statements == []


@pytest.mark.unit
def test_reader_refuses_an_unsafe_relation() -> None:
    from omnimarket.routing.dod_overlay import PostgresDodOutcomeReader

    with pytest.raises(ValueError, match="unsafe relation"):
        PostgresDodOutcomeReader(
            "postgresql://x", "postgresql://y", verdict_relation="runs; drop"
        )


@pytest.mark.unit
def test_reader_failure_returns_none_so_routing_stays_static(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.routing.dod_overlay import (
        PostgresDodOutcomeReader,
        resolve_dod_overlay,
    )

    run = str(uuid.uuid4())
    verdict_conn = _FakeConn(
        rows=[
            {
                "verdict_correlation_id": "v1",
                "delegation_correlation_id": run,
                "verdict_status": "verified",
                "verdict_outcome": "done",
                "verdict_completed_at": BASE_TIME,
            }
        ]
    )
    delegation_conn = _FakeConn(fail_on_select=True)
    _patch_connect(
        monkeypatch, {VERDICT_DSN: verdict_conn, DELEGATION_DSN: delegation_conn}
    )
    reader = PostgresDodOutcomeReader(DELEGATION_DSN, VERDICT_DSN)

    assert resolve_dod_overlay(reader, task_type=TASK_TYPE, tenant_id=TENANT_ID) is None
    assert delegation_conn.rollbacks == 1
    assert delegation_conn.autocommit is True


@pytest.mark.unit
def test_reader_missing_tenant_under_enforcement_raises_before_any_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.projection import tenant_isolation
    from omnimarket.projection.tenant_isolation import TenantContextMissingError
    from omnimarket.routing.dod_overlay import resolve_dod_overlay

    class _Settings:
        onex_tenant_id = ""
        enforce_tenant_isolation = True

    monkeypatch.setattr(tenant_isolation, "get_settings", lambda: _Settings())
    calls: list[str] = []

    class _Reader:
        def read_dod_outcomes(
            self, *, task_type: str, tenant_id: str
        ) -> list[dict[str, object]]:
            calls.append(task_type)
            return []

    with pytest.raises(TenantContextMissingError):
        resolve_dod_overlay(_Reader(), task_type=TASK_TYPE, tenant_id=None)
    assert calls == []


@pytest.mark.unit
def test_reader_tenant_slug_maps_to_its_uuid_and_unknown_slug_skips_the_read() -> None:
    from omnimarket.projection.tenant_isolation import UnmappedTenantIdentityError
    from omnimarket.routing.dod_overlay import (
        resolve_dod_overlay,
        resolve_dod_read_tenant,
    )

    assert resolve_dod_read_tenant("omninode") == TENANT_ID
    assert resolve_dod_read_tenant(TENANT_ID.upper()) == TENANT_ID
    with pytest.raises(UnmappedTenantIdentityError):
        resolve_dod_read_tenant("not-a-known-tenant")

    calls: list[str] = []

    class _Reader:
        def read_dod_outcomes(
            self, *, task_type: str, tenant_id: str
        ) -> list[dict[str, object]]:
            calls.append(tenant_id)
            return []

    assert (
        resolve_dod_overlay(
            _Reader(), task_type=TASK_TYPE, tenant_id="not-a-known-tenant"
        )
        is None
    )
    assert calls == []


@pytest.mark.unit
def test_reader_resolution_needs_both_lane_dsns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.routing.dod_overlay import (
        PostgresDodOutcomeReader,
        resolve_dod_outcome_reader,
    )

    monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
    monkeypatch.delenv("OMNINODE_INTERNAL_DB_URL", raising=False)
    assert resolve_dod_outcome_reader() is None
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", DELEGATION_DSN)
    assert resolve_dod_outcome_reader() is None
    monkeypatch.setenv("OMNINODE_INTERNAL_DB_URL", VERDICT_DSN)
    assert isinstance(resolve_dod_outcome_reader(), PostgresDodOutcomeReader)


@pytest.mark.unit
def test_port_selection_wires_the_dod_read_into_the_local_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4: the bus-less port consults the same DoD signal as the deployed lane."""
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
        evidence_db_resolution,
    )
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_selection import (
        select_delegation_dispatch_port,
    )
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
    from omnimarket.routing import dod_overlay

    rows = [_row(verdict_outcome="refused") for _ in range(5)]

    class _Reader:
        def read_dod_outcomes(
            self, *, task_type: str, tenant_id: str
        ) -> list[dict[str, object]]:
            return [r for r in rows if r["task_type"] == task_type]

    monkeypatch.setattr(dod_overlay, "resolve_dod_outcome_reader", lambda: _Reader())
    monkeypatch.setattr(
        evidence_db_resolution,
        "resolve_local_delegation_evidence_db",
        lambda: InmemoryDatabaseAdapter(),
    )
    monkeypatch.delenv("DELEGATION_ROI_MIN_SAMPLES", raising=False)
    monkeypatch.delenv("DELEGATION_ROI_SUCCESS_FLOOR", raising=False)
    _clear_env(monkeypatch)

    port = select_delegation_dispatch_port(None)
    assert isinstance(port, LocalDelegationDispatchPort)
    overlay = port._roi_overlay_reader(TASK_TYPE)

    assert overlay is not None
    assert overlay.suppressed_tiers == frozenset({"local"})


@pytest.mark.unit
def test_port_selection_without_a_dsn_leaves_the_local_port_static(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
        evidence_db_resolution,
    )
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_selection import (
        select_delegation_dispatch_port,
    )
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

    monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
    monkeypatch.setattr(
        evidence_db_resolution,
        "resolve_local_delegation_evidence_db",
        lambda: InmemoryDatabaseAdapter(),
    )

    port = select_delegation_dispatch_port(None)

    assert isinstance(port, LocalDelegationDispatchPort)
    assert port._roi_overlay_reader(TASK_TYPE) is None
