# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19977 AC4 and AC5: the delegation refreshes its own metering rows.

In mode 1 the fold runs in-process at the end of each ``onex delegate``: once
the terminal ``delegation_events`` row is durable, the local dispatch port
issues the metering node's own fold request, on the node's own command topic
(``onex.cmd.omnimarket.refresh-metering-summary.v1``), for the run's UTC day
plus the all row. When the resolved baseline or the pricing manifest version
differs from what the tenant's rows were folded against, the same command goes
out for the whole tenant (``days=None``) instead (ruling D-A).

These run over the real local dispatch path -- ``HandlerDelegateSkill`` and
``LocalDelegationDispatchPort`` with their default evidence store -- on a fresh
HOME, with only the provider stubbed at the transport boundary.

Failure modes each test is written against:

* no hook: no rows until ``onex metering`` runs (which no longer folds);
* the day row without the all row, or the reverse;
* the wrong UTC day at 23:59:59Z / 00:00:00Z, or a day row other than the
  run's rewritten;
* the run missing from its own row (refresh before the terminal row is durable,
  or ``as_of`` at or before the terminal time);
* a second identical terminal delivery changing a byte;
* a failed run summed, or not counted;
* another tenant's rows rewritten;
* a refresh failure failing a successful delegation, or passing silently;
* a refresh with no durable terminal row, or into a store that is not the
  local SQLite evidence file;
* a late refresh for an earlier run overwriting a newer row, and two refreshes
  interleaving;
* a manifest or baseline change that refreshes only one day, a publish on any
  topic but the node's own, and a whole-tenant refold on every delegation.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from click.testing import CliRunner

from omnimarket import pricing
from omnimarket.cli.cli_metering import metering_command
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.inference.provider_quota_state import SqliteProviderQuotaReader
from omnimarket.local_deployment import tenant_identity
from omnimarket.models.delegation.local_credential_refusal import (
    EnumLocalCredentialRefusalReason,
    ModelLocalCredentialRefusal,
)
from omnimarket.models.delegation.wire import (
    model_delegate_skill_terminal_projection as terminal_module,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call,
    transport,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)
from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    ModelMeteringRecord,
)
from omnimarket.nodes.node_projection_metering_summary import (
    HandlerProjectionMeteringSummary,
    ModelMeteringSummaryFoldRequest,
)
from omnimarket.nodes.node_projection_metering_summary.baseline import resolve_baseline
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    store_rows,
)
from omnimarket.projection import sqlite_database, sqlite_metering_summary
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.routing import delegation_backend_resolution

pytestmark = pytest.mark.unit

_TOKENS_IN = 213
_TOKENS_OUT = 6
OTHER_TENANT = "11111111-2222-4333-8444-555555555555"
LAST_SECOND = datetime(2026, 10, 4, 23, 59, 59, tzinfo=UTC)
MIDNIGHT = datetime(2026, 10, 5, 0, 0, 0, tzinfo=UTC)
_CONTRACT = (
    Path(sqlite_metering_summary.__file__).resolve().parents[1]
    / "nodes"
    / "node_projection_metering_summary"
    / "contract.yaml"
)


def _refresh_topic() -> str:
    """The node's own command topic, read from its contract (not a literal)."""
    (topic,) = yaml.safe_load(_CONTRACT.read_text())["event_bus"]["subscribe_topics"]
    return str(topic)


@pytest.fixture(autouse=True)
def _clear_health_cache() -> None:
    handler_llm_delegation_call._health_cache.clear()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A fresh HOME: no store, no identity, nothing folded yet."""
    root = tmp_path / "home"
    store = root / ".omninode" / "delegation" / "delegation.sqlite"
    store.parent.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setattr(sqlite_database, "_DEFAULT_EVIDENCE_DB_PATH", store)
    monkeypatch.setattr(tenant_identity, "default_evidence_db_path", lambda: store)
    tenant_identity.reset_local_tenant_identity_cache()
    _patch_provider(monkeypatch)
    yield store
    tenant_identity.reset_local_tenant_identity_cache()


def _patch_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    backends: list[dict[str, object]] = [
        {
            "backend_id": "local-coder",
            "endpoint_url": "http://inference.example:8000/v1/chat/completions",
            "model_name": "Qwen3.6-35B-A3B",
            "tier": "local",
            "max_tokens": 65536,
            "timeout_ms": 300000,
            "capabilities": ["code_generation"],
        }
    ]
    monkeypatch.setattr(
        delegation_backend_resolution, "load_bifrost_backends", lambda **_: backends
    )

    def fake_post(**_: Any) -> transport.ModelTransportResponse:
        return transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "choices": [
                    {
                        "message": {
                            "content": "### ANSWER\ndef reverse(s): return s[::-1]"
                        }
                    }
                ],
                "model": "Qwen3.6-35B-A3B",
                "usage": {
                    "prompt_tokens": _TOKENS_IN,
                    "completion_tokens": _TOKENS_OUT,
                    "total_tokens": _TOKENS_IN + _TOKENS_OUT,
                },
            },
            latency_ms=42,
        )

    monkeypatch.setattr(transport, "probe_health", lambda *_a, **_k: True)
    monkeypatch.setattr(transport, "post_chat_completion", fake_post)


def _at(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    """Pin the terminal event's time (the row's created_at) to ``instant``."""

    class _Clock:
        @staticmethod
        def now(tz: tzinfo | None = None) -> datetime:
            return instant if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(terminal_module, "datetime", _Clock)


def _missing_key_effect(
    request: ModelLlmDelegationCallRequest,
) -> ModelLlmDelegationCallResult:
    """The provider key is absent: the run fails before any token is spent."""
    refusal = ModelLocalCredentialRefusal(
        reason=EnumLocalCredentialRefusalReason.CREDENTIAL_ABSENT,
        credential_ref="llm.omn19977nosuchprovider.api_key",
        credential_env="OMN19977_EXAMPLE_ENV",
        backend_ref=request.endpoint_ref,
        model_id=request.model_id,
        correlation_id=request.correlation_id,
        detail="no key",
    )
    return ModelLlmDelegationCallResult(
        request_id=request.request_id,
        success=False,
        failure_class=refusal.failure_class,
        error_message=refusal.message,
        credential_refusal=refusal,
    )


def _delegate(**port_kwargs: Any) -> Any:
    port = LocalDelegationDispatchPort(effect_process_boundary=False, **port_kwargs)
    handler = HandlerDelegateSkill(None, dispatch_port=port)
    return asyncio.run(
        handler.handle(
            ModelDelegateSkillRequest(
                prompt="reverse a string",
                task_type="code_generation",
                source="claude-code",
            )
        )
    )


def _rows(
    store: Path, tenant: str | None = None
) -> dict[tuple[str, str, str], dict[str, Any]]:
    conn = sqlite3.connect(store)
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'metering_summary'"
        ).fetchone()
        if found is None:
            return {}
        query = "SELECT * FROM metering_summary"
        params: tuple[str, ...] = ()
        if tenant is not None:
            query += " WHERE tenant_id = ?"
            params = (tenant,)
        return {
            (r["window_kind"], r["window_start"], r["baseline_model"]): dict(r)
            for r in conn.execute(query, params).fetchall()
        }
    finally:
        conn.close()


def _tenant(store: Path) -> str:
    identity = tenant_identity.read_local_tenant_identity(db_path=store)
    assert identity is not None
    return str(identity.tenant_uuid)


def _baseline() -> str:
    return pricing.resolve_baseline_model(overlay={}, store={}).model


class _Recording:
    """Records each published fold request, then delivers it in-process."""

    def __init__(self, store: Path, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, ModelMeteringSummaryFoldRequest]] = []
        self._store = store
        self._fail = fail

    def publish(self, topic: str, request: ModelMeteringSummaryFoldRequest) -> None:
        self.calls.append((topic, request))
        if self._fail:
            raise RuntimeError("metering store locked by another writer")
        sqlite_metering_summary.InProcessMeteringRefreshPublisher(self._store).publish(
            topic, request
        )


# -- AC4: end-of-delegate refresh ---------------------------------------------


def test_one_delegation_on_a_fresh_home_writes_its_day_and_all_rows(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _at(monkeypatch, LAST_SECOND)
    response = _delegate()
    assert response.status == "completed"

    tenant = _tenant(home)
    baseline = _baseline()
    rows = _rows(home, tenant)
    assert set(rows) == {("all", "", baseline), ("day", "2026-10-04", baseline)}
    for row in rows.values():
        assert datetime.fromisoformat(row["as_of"]) > LAST_SECOND
        assert row["runs_total"] == 1
        assert row["runs_measured"] == 1
        assert row["tokens_in"] == _TOKENS_IN
        assert row["tokens_out"] == _TOKENS_OUT
        assert row["savings_usd"] is not None

    # Without any refresh by the reader, the CLI prints that row.
    result = CliRunner().invoke(metering_command, ["--json"])
    assert result.exit_code == 0, result.output
    printed = json.loads(result.stdout)
    assert printed["tenant_id"] == tenant
    assert printed["runs_total"] == 1
    assert printed["as_of"] == rows[("all", "", baseline)]["as_of"]
    assert printed["savings_usd"] == rows[("all", "", baseline)]["savings_usd"]


def test_runs_either_side_of_utc_midnight_land_on_their_own_days(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _baseline()
    _at(monkeypatch, LAST_SECOND)
    _delegate()
    tenant = _tenant(home)
    first_day = _rows(home, tenant)[("day", "2026-10-04", baseline)]

    _at(monkeypatch, MIDNIGHT)
    _delegate()
    rows = _rows(home, tenant)

    assert rows[("day", "2026-10-04", baseline)] == first_day
    assert rows[("day", "2026-10-05", baseline)]["runs_total"] == 1
    assert rows[("day", "2026-10-05", baseline)]["window_end"] == (
        "2026-10-06T00:00:00+00:00"
    )
    assert rows[("all", "", baseline)]["runs_total"] == 2
    assert datetime.fromisoformat(rows[("all", "", baseline)]["as_of"]) > MIDNIGHT


def test_a_second_identical_terminal_delivery_changes_no_byte(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _at(monkeypatch, LAST_SECOND)
    port = LocalDelegationDispatchPort(effect_process_boundary=False)
    correlation_id = uuid4()

    def deliver() -> None:
        asyncio.run(
            port.dispatch(
                prompt="reverse a string",
                task_type="code_generation",
                correlation_id=correlation_id,
                max_tokens=256,
                source_file_path=None,
                source_session_id=None,
                wait=True,
                execution_timeout_seconds=240,
                terminal_delivery_margin_seconds=60,
                quality_contract_mode="extend_task_class",
                acceptance_criteria=(),
                tenant_id=None,
            )
        )

    deliver()
    first = _rows(home)
    assert first
    deliver()
    assert _rows(home) == first


def test_a_failed_run_counts_in_runs_and_in_no_sum(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _at(monkeypatch, LAST_SECOND)
    response = _delegate(effect_handler=_missing_key_effect)
    assert response.status == "failed"

    rows = _rows(home, _tenant(home))
    all_row = rows[("all", "", _baseline())]
    assert all_row["runs_total"] == 1
    assert all_row["runs_measured"] == 0
    assert all_row["runs_unknown_tokens"] == 1
    assert all_row["tokens_in"] == 0
    assert all_row["tokens_out"] == 0
    assert all_row["spend_usd"] is None
    assert all_row["counterfactual_usd"] is None
    assert all_row["savings_usd"] is None

    # A successful run the same day is summed alone.
    _at(monkeypatch, LAST_SECOND - timedelta(hours=1))
    _delegate()
    all_row = _rows(home, _tenant(home))[("all", "", _baseline())]
    assert all_row["runs_total"] == 2
    assert all_row["runs_measured"] == 1
    assert all_row["tokens_in"] == _TOKENS_IN


def test_another_tenants_rows_are_untouched(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = HandlerProjectionMeteringSummary().handle(
        ModelMeteringSummaryFoldRequest(
            tenant_id=OTHER_TENANT,
            records=(
                ModelMeteringRecord(
                    correlation_id="other",
                    occurred_at=LAST_SECOND - timedelta(days=2),
                    model="w",
                    tokens_in=10,
                    tokens_out=10,
                    spend_usd=Decimal("1"),
                ),
            ),
            baseline=None,
            baseline_model=_baseline(),
            as_of=LAST_SECOND - timedelta(days=1),
        )
    )
    store_rows(SqliteDatabaseAdapter(home), other.rows)
    before = _rows(home, OTHER_TENANT)
    assert before

    _at(monkeypatch, LAST_SECOND)
    _delegate()
    assert _rows(home, OTHER_TENANT) == before
    assert _rows(home, _tenant(home))


def test_a_refresh_failure_is_surfaced_and_the_delegation_still_succeeds(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _at(monkeypatch, LAST_SECOND)
    publisher = _Recording(home, fail=True)
    response = _delegate(metering_refresh_publisher=publisher)

    assert response.status == "completed"
    assert len(publisher.calls) == 1
    err = capsys.readouterr().err
    assert "metering summary was not refreshed" in err
    assert str(response.correlation_id) in err
    assert "onex metering" in err
    assert any(
        "metering summary" in r.getMessage() and r.levelname == "WARNING"
        for r in caplog.records
    )


class _UnwrittenProjection:
    """The terminal projection on a writable store, whose row did not land."""

    def __init__(self, *, raises: bool) -> None:
        self._raises = raises

    def project_delegate_skill_terminal(self, *_: Any, **__: Any) -> Any:
        from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
            ModelProjectionResult,
        )

        if self._raises:
            raise RuntimeError("database is locked")
        return ModelProjectionResult(rows_upserted=0)


@pytest.mark.parametrize("raises", [False, True], ids=["zero-rows", "raised"])
def test_no_refresh_when_the_terminal_row_was_not_written(
    home: Path, monkeypatch: pytest.MonkeyPatch, raises: bool
) -> None:
    publisher = _Recording(home)
    attempts: list[object] = []
    monkeypatch.setattr(
        port_module,
        "refresh_metering_after_terminal",
        lambda *a, **k: attempts.append((a, k)),
    )
    _at(monkeypatch, LAST_SECOND)
    response = _delegate(
        projection_handler=_UnwrittenProjection(raises=raises),
        metering_refresh_publisher=publisher,
    )
    assert response.status == "completed"
    assert attempts == []
    assert publisher.calls == []


def test_an_unwritable_store_still_returns_the_delegation(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    publisher = _Recording(home)
    _at(monkeypatch, LAST_SECOND)
    response = _delegate(
        evidence_db_path=blocker / "delegation.sqlite",
        metering_refresh_publisher=publisher,
    )
    assert response.status == "completed"
    assert publisher.calls == []


def test_a_store_that_is_not_the_local_sqlite_file_is_not_folded_locally(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overlay-bound shared store has its own lane writer (OMN-14015)."""

    class _SharedStore:
        db_path = None

    class _Projection:
        def project_delegate_skill_terminal(self, *_: Any, **__: Any) -> Any:
            from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
                ModelProjectionResult,
            )

            return ModelProjectionResult(rows_upserted=1)

    publisher = _Recording(home)
    attempts: list[object] = []
    monkeypatch.setattr(
        port_module,
        "refresh_metering_after_terminal",
        lambda *a, **k: attempts.append((a, k)),
    )
    _at(monkeypatch, LAST_SECOND)
    response = _delegate(
        evidence_db=_SharedStore(),
        projection_handler=_Projection(),
        metering_refresh_publisher=publisher,
        # The quota read would otherwise bind through a lane overlay.
        quota_reader=SqliteProviderQuotaReader(home),
    )
    assert response.status == "completed", response.model_dump_json()
    assert publisher.calls == []
    assert attempts == []


def test_a_late_refresh_for_an_earlier_run_does_not_regress_the_row(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _at(monkeypatch, LAST_SECOND)
    _delegate()
    _at(monkeypatch, MIDNIGHT)
    _delegate()
    tenant = _tenant(home)
    newest = _rows(home, tenant)

    # The first run's refresh arriving after the second run's: it folds the
    # same snapshot (as_of follows the newest recorded run, not its own
    # terminal), so nothing moves back and no figure changes.
    sqlite_metering_summary.refresh_metering_after_terminal(
        home, tenant_id=tenant, terminal_at=LAST_SECOND
    )
    after = _rows(home, tenant)
    assert set(after) == set(newest)
    for key, row in after.items():
        assert row["as_of"] == newest[("all", "", key[2])]["as_of"], key
        changed = {k for k in row if row[k] != newest[key][k]}
        assert changed <= {"as_of", "summary_json"}, (key, changed)
        assert json.loads(row["summary_json"]) | {"window": None} == json.loads(
            newest[key]["summary_json"]
        ) | {"window": None}


def test_two_refreshes_of_one_store_do_not_interleave(home: Path) -> None:
    tenant = str(tenant_identity.mint_local_tenant_identity(db_path=home).tenant_uuid)
    SqliteDatabaseAdapter(home).upsert(
        "delegation_events",
        "correlation_id",
        {
            "correlation_id": "r",
            "created_at": LAST_SECOND.isoformat(),
            "delegated_to": "w",
            "model_name": "w",
            "task_type": "",
            "tokens_input": 1,
            "tokens_output": 1,
            "cost_usd": "0",
            "cost_savings_usd": None,
        },
    )
    failures: list[Exception] = []
    published = threading.Event()

    class _Signal:
        def publish(self, topic: str, request: Any) -> None:
            published.set()

    lock = sqlite_metering_summary.metering_refresh_lock(home)
    with lock:

        def refresh() -> None:
            try:
                sqlite_metering_summary.refresh_metering_after_terminal(
                    home, tenant_id=tenant, terminal_at=LAST_SECOND, publisher=_Signal()
                )
            except Exception as exc:
                failures.append(exc)

        worker = threading.Thread(target=refresh)
        worker.start()
        assert not published.wait(0.5)
    assert published.wait(10), failures
    worker.join(10)
    assert failures == []


def test_a_refresh_lock_held_by_a_stuck_writer_does_not_hang_the_delegation(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refresh that never gets the lock gives up, and says so; the run returns."""
    monkeypatch.setattr(
        sqlite_metering_summary, "METERING_REFRESH_LOCK_TIMEOUT_SECONDS", 0.5
    )
    _at(monkeypatch, LAST_SECOND)
    outcome: list[Any] = []
    with sqlite_metering_summary.metering_refresh_lock(home):
        worker = threading.Thread(
            target=lambda: outcome.append(_delegate()), daemon=True
        )
        worker.start()
        worker.join(30)
        finished = not worker.is_alive()
    worker.join(30)
    assert finished, "the delegation waited on the metering lock"
    assert outcome[0].status == "completed"
    err = capsys.readouterr().err
    assert "metering summary was not refreshed" in err
    assert "TimeoutError" in err
    assert _rows(home) == {}


# -- AC5 (D-A): a baseline or manifest change refreshes the whole tenant ------


def test_without_a_change_only_the_runs_day_is_refolded(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _at(monkeypatch, LAST_SECOND)
    _delegate()
    publisher = _Recording(home)
    _at(monkeypatch, MIDNIGHT)
    _delegate(metering_refresh_publisher=publisher)

    ((topic, request),) = publisher.calls
    assert topic == _refresh_topic()
    assert request.days == frozenset({MIDNIGHT.date()})
    assert request.tenant_id == _tenant(home)


def test_a_manifest_version_change_publishes_the_refresh_for_the_whole_tenant(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _baseline()
    for instant in (LAST_SECOND - timedelta(days=1), LAST_SECOND):
        _at(monkeypatch, instant)
        _delegate()
    tenant = _tenant(home)
    old_version = _rows(home, tenant)[("all", "", baseline)]["pricing_manifest_version"]
    assert old_version is not None

    real = resolve_baseline

    def next_manifest(model: str) -> Any:
        resolved = real(model)
        assert resolved is not None
        return resolved.model_copy(update={"pricing_manifest_version": "next"})

    monkeypatch.setattr(
        "omnimarket.projection.sqlite_metering_summary.resolve_baseline", next_manifest
    )
    publisher = _Recording(home)
    _at(monkeypatch, MIDNIGHT)
    _delegate(metering_refresh_publisher=publisher)

    ((topic, request),) = publisher.calls
    assert topic == _refresh_topic()
    assert request.days is None
    assert request.tenant_id == tenant
    assert request.baseline_model == baseline
    rows = _rows(home, tenant)
    assert set(rows) == {
        ("all", "", baseline),
        ("day", "2026-10-03", baseline),
        ("day", "2026-10-04", baseline),
        ("day", "2026-10-05", baseline),
    }
    assert {r["pricing_manifest_version"] for r in rows.values()} == {"next"}
    assert len({r["as_of"] for r in rows.values()}) == 1


def test_a_baseline_model_change_publishes_the_refresh_for_the_whole_tenant(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _baseline()
    for instant in (LAST_SECOND - timedelta(days=1), LAST_SECOND):
        _at(monkeypatch, instant)
        _delegate()
    tenant = _tenant(home)
    before = {k: v for k, v in _rows(home, tenant).items() if k[2] == first}

    monkeypatch.setattr(pricing, "DEFAULT_BASELINE_MODEL", "claude-opus-4-6")
    assert _baseline() == "claude-opus-4-6" != first
    publisher = _Recording(home)
    _at(monkeypatch, MIDNIGHT)
    _delegate(metering_refresh_publisher=publisher)

    ((topic, request),) = publisher.calls
    assert topic == _refresh_topic()
    assert request.days is None
    assert request.baseline_model == "claude-opus-4-6"
    rows = _rows(home, tenant)
    assert {k for k in rows if k[2] == "claude-opus-4-6"} == {
        ("all", "", "claude-opus-4-6"),
        ("day", "2026-10-03", "claude-opus-4-6"),
        ("day", "2026-10-04", "claude-opus-4-6"),
        ("day", "2026-10-05", "claude-opus-4-6"),
    }
    assert rows[("all", "", "claude-opus-4-6")]["runs_total"] == 3
    # The rows folded under the previous baseline are another key, left as they were.
    assert {k: v for k, v in rows.items() if k[2] == first} == before
