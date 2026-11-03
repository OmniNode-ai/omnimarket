# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19996: a key's Credentials row records when it was last used in a run.

Failure modes, each with a test below:
L1 contract: both terminal topics are subscribed and routed.
L2 exposure: the projection exposure lists last_used_at.
L3 migration: the SQL adds LAST_USED_AT if it is missing.
L4 terminal: a terminal event writes exactly one UPDATE with the time.
L5 monotonic: the UPDATE only moves last_used_at forward.
L6 seam: the runner-seam envelope also supplies the timestamp.
L7 missing ref: a terminal with no secret_ref writes nothing.
L8 missing time: a terminal with no envelope time writes nothing.
L9 store: apply_credential_last_used on real SQLite is monotonic.
L10 port: LocalDelegationDispatchPort stamps the credential on success.
L11 order: mixed offsets and precisions never lower the stored time, on
    rows written before this rule too.
L12 tenant: a terminal for tenant B naming tenant A's ref changes nothing;
    the local store (no tenant given) stamps the key whatever its tenant.
L13 time authority: the terminal's own emitted_at wins over the envelope time,
    as on the delegation_events row.
L14 dispatch: a subscribed topic that is not a delegation terminal is not
    read as one.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection import (
    HandlerTenantCredentialsProjectionRunner,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    CREDENTIALS_TABLE,
    apply_credential_last_used,
    apply_credential_registered,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[1]
_NODE_DIR = _REPO_ROOT / "src/omnimarket/nodes/node_projection_tenant_credentials"
TOPIC_COMPLETED = "onex.evt.omnimarket.delegate-skill-completed.v1"
TOPIC_FAILED = "onex.evt.omnimarket.delegate-skill-failed.v1"
EXPOSURE_TOPIC = "onex.snapshot.projection.tenant-credentials.v1"
MIGRATION = _NODE_DIR / "migrations/0010_tenant_credentials_last_used_at.sql"
REF = "cred_omninode_openrouter_" + "a" * 32
T1 = "2026-10-28T04:00:00+00:00"
T0 = "2026-10-27T04:00:00+00:00"
TENANT = "omninode"


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> HandlerTenantCredentialsProjectionRunner:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    r = HandlerTenantCredentialsProjectionRunner()
    r._db = db
    monkeypatch.setattr(r, "publish_snapshot_delta", AsyncMock(return_value=True))
    return r


def _writes(runner):
    return [
        call
        for call in runner._db.execute.await_args_list
        if "tenant_inference_credentials" in str(call.args[0])
    ]


def _meta(topic):
    return MessageMeta(partition=0, offset=1, fallback_id="fb", topic=topic)


def _terminal(**overrides):
    data = {
        "correlation_id": "c0ffee00-0000-4000-8000-000000000001",
        "secret_ref": REF,
        "_envelope_timestamp": T1,
    }
    data.update(overrides)
    return data


def test_l1_contract_topics_subscribed_and_routed():
    contract = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text())
    subscribe = set(contract["event_bus"]["subscribe_topics"])
    routed = {e["topic"] for e in contract["handler_routing"]["handlers"]}
    assert TOPIC_COMPLETED in subscribe
    assert TOPIC_FAILED in subscribe
    assert TOPIC_COMPLETED in routed
    assert TOPIC_FAILED in routed


def test_l2_exposure_lists_last_used_at():
    contract = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text())
    exposures = contract["projection_api"]["exposures"]
    match = [e for e in exposures if e.get("topic") == EXPOSURE_TOPIC]
    assert match, "exposure for EXPOSURE_TOPIC not found"
    assert "last_used_at" in match[0]["columns"]


def test_l3_migration_adds_last_used_at():
    assert MIGRATION.exists()
    text = MIGRATION.read_text().upper()
    assert "ADD COLUMN IF NOT EXISTS LAST_USED_AT TIMESTAMPTZ" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", [TOPIC_COMPLETED, TOPIC_FAILED])
async def test_l4_terminal_writes_one_update(runner, topic):
    data = _terminal()
    await runner.project_event(topic, data, _meta(topic))
    writes = _writes(runner)
    assert len(writes) == 1
    sql = str(writes[0].args[0]).strip()
    assert sql.startswith("UPDATE tenant_inference_credentials")
    assert "last_used_at" in sql
    assert "INSERT" not in sql
    assert REF in writes[0].args
    assert datetime.fromisoformat(T1) in writes[0].args


@pytest.mark.asyncio
async def test_l5_update_only_moves_forward(runner):
    data = _terminal()
    await runner.project_event(TOPIC_COMPLETED, data, _meta(TOPIC_COMPLETED))
    writes = _writes(runner)
    assert len(writes) == 1
    sql = str(writes[0].args[0])
    assert "last_used_at IS NULL OR last_used_at <" in sql


@pytest.mark.asyncio
async def test_l6_runner_seam_envelope(runner):
    data = {
        "correlation_id": "c0ffee00-0000-4000-8000-000000000001",
        "secret_ref": REF,
        "_envelope": {"envelope_timestamp": T1},
    }
    await runner.project_event(TOPIC_COMPLETED, data, _meta(TOPIC_COMPLETED))
    writes = _writes(runner)
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_l7_missing_secret_ref_writes_nothing(runner):
    data = _terminal(secret_ref=None)
    await runner.project_event(TOPIC_COMPLETED, data, _meta(TOPIC_COMPLETED))
    assert _writes(runner) == []
    data2 = _terminal()
    del data2["secret_ref"]
    await runner.project_event(TOPIC_FAILED, data2, _meta(TOPIC_FAILED))
    assert _writes(runner) == []


@pytest.mark.asyncio
async def test_l8_missing_envelope_time_writes_nothing(runner):
    data = {
        "correlation_id": "c0ffee00-0000-4000-8000-000000000001",
        "secret_ref": REF,
    }
    await runner.project_event(TOPIC_COMPLETED, data, _meta(TOPIC_COMPLETED))
    assert _writes(runner) == []


def test_l9_store_monotonic_on_real_sqlite(tmp_path):
    db = SqliteDatabaseAdapter(tmp_path / "store.sqlite")
    apply_credential_registered(
        {
            "tenant_id": "omninode",
            "provider": "openrouter",
            "name": "k",
            "api_key_ref": REF,
        },
        db,
    )
    assert apply_credential_last_used(REF, T1, db, tenant_id=TENANT) is True
    rows = db.query(CREDENTIALS_TABLE, {"api_key_ref": REF})
    assert len(rows) == 1
    assert rows[0]["last_used_at"] == T1
    assert apply_credential_last_used(REF, T0, db, tenant_id=TENANT) is False
    rows = db.query(CREDENTIALS_TABLE, {"api_key_ref": REF})
    assert rows[0]["last_used_at"] == T1
    unknown = "cred_unknown_" + "b" * 32
    assert apply_credential_last_used(unknown, T1, db, tenant_id=TENANT) is False
    assert db.query(CREDENTIALS_TABLE, {"api_key_ref": unknown}) == []
    assert apply_credential_last_used(None, T1, db, tenant_id=TENANT) is False


def test_l10_local_port_end_to_end(tmp_path):
    # The local store's key carries the tenant given to `onex secret` (dev),
    # while the run is recorded under another identity (omninode).
    db_path = tmp_path / "delegation.sqlite"
    db = SqliteDatabaseAdapter(db_path)
    apply_credential_registered(
        {
            "tenant_id": "dev",
            "provider": "openrouter",
            "name": "k",
            "api_key_ref": REF,
        },
        db,
    )
    port = LocalDelegationDispatchPort(evidence_db_path=db_path)
    correlation_id = uuid.uuid4()
    port._project_evidence(
        correlation_id=correlation_id,
        task_type="document",
        endpoint_ref="local",
        model_id="model-local",
        result=ModelLlmDelegationCallResult(
            request_id="req-omn19996",
            success=True,
            content="an answer",
            tokens_in=11,
            tokens_out=22,
            latency_ms=33,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
            secret_ref=REF,
        ),
        prompt="p",
        source_session_id=None,
        tenant_id="omninode",
        quality_passed=True,
        failure_message="",
        cost_usd=Decimal("0"),
        baseline_savings=None,
        escalation_count=0,
        attempts=[],
        actual_score=None,
        required_bar=None,
    )
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT timestamp FROM delegation_events WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchone()
        assert row is not None
        timestamp = row[0]
        cred = conn.execute(
            "SELECT last_used_at FROM "
            "tenant_inference_credentials "
            "WHERE api_key_ref = ?",
            (REF,),
        ).fetchone()
        assert cred is not None
        assert cred[0] is not None
        assert cred[0] == timestamp
    finally:
        conn.close()


def _registered_store(tmp_path, tenant=TENANT):
    db = SqliteDatabaseAdapter(tmp_path / "store.sqlite")
    apply_credential_registered(
        {
            "tenant_id": tenant,
            "provider": "openrouter",
            "name": "k",
            "api_key_ref": REF,
        },
        db,
    )
    return db


def _stored_last_used(db):
    return db.query(CREDENTIALS_TABLE, {"api_key_ref": REF})[0]["last_used_at"]


def test_l11_mixed_offsets_and_precisions_never_lower(tmp_path):
    db = _registered_store(tmp_path)
    assert apply_credential_last_used(REF, T1, db, tenant_id=TENANT) is True
    # 05:00+02:00 is 03:00Z, an hour before T1, though its text sorts after.
    assert (
        apply_credential_last_used(
            REF, "2026-10-28T05:00:00+02:00", db, tenant_id=TENANT
        )
        is False
    )
    assert _stored_last_used(db) == T1
    # The same instant as T1 in another offset is not later.
    assert (
        apply_credential_last_used(
            REF, "2026-10-28T06:00:00+02:00", db, tenant_id=TENANT
        )
        is False
    )
    # One microsecond later, written with a precision T1 does not have.
    later = "2026-10-28T04:00:00.000001+00:00"
    assert apply_credential_last_used(REF, later, db, tenant_id=TENANT) is True
    assert _stored_last_used(db) == later
    assert apply_credential_last_used(REF, T1, db, tenant_id=TENANT) is False
    assert _stored_last_used(db) == later


def test_l11_row_stored_in_another_offset_still_orders_by_instant(tmp_path):
    db = _registered_store(tmp_path)
    # A row written before this rule, in an offset whose text sorts after T1
    # although its instant (03:30Z) is earlier.
    conn = sqlite3.connect(tmp_path / "store.sqlite")
    try:
        conn.execute(
            "UPDATE tenant_inference_credentials SET last_used_at = ? "
            "WHERE api_key_ref = ?",
            ("2026-10-28T09:00:00+05:30", REF),
        )
        conn.commit()
    finally:
        conn.close()
    assert apply_credential_last_used(REF, T1, db, tenant_id=TENANT) is True
    assert _stored_last_used(db) == T1


def test_l11_naive_time_is_refused(tmp_path):
    db = _registered_store(tmp_path)
    with pytest.raises(ValueError, match="no UTC offset"):
        apply_credential_last_used(REF, "2026-10-28T04:00:00", db, tenant_id=TENANT)
    assert _stored_last_used(db) is None


def test_l12_other_tenant_store_write_changes_nothing(tmp_path):
    db = _registered_store(tmp_path, tenant="tenant-a")
    assert apply_credential_last_used(REF, T1, db, tenant_id="tenant-b") is False
    assert _stored_last_used(db) is None
    assert apply_credential_last_used(REF, T1, db, tenant_id="tenant-a") is True
    assert _stored_last_used(db) == T1


@pytest.mark.asyncio
async def test_l12_runner_update_is_scoped_to_the_terminal_tenant(runner):
    data = _terminal(tenant_id="tenant-b")
    await runner.project_event(TOPIC_COMPLETED, data, _meta(TOPIC_COMPLETED))
    writes = _writes(runner)
    assert len(writes) == 1
    sql = " ".join(str(writes[0].args[0]).split())
    assert "WHERE api_key_ref = $1 AND tenant_id = $3" in sql
    assert writes[0].args[1:] == (REF, datetime.fromisoformat(T1), "tenant-b")
    assert writes[0].kwargs["tenant"] == "tenant-b"


def test_l12_local_store_without_tenant_stamps_any_tenant_key(tmp_path):
    db = _registered_store(tmp_path, tenant="dev")
    assert apply_credential_last_used(REF, T1, db) is True
    assert _stored_last_used(db) == T1
    assert apply_credential_last_used(REF, T0, db) is False
    assert _stored_last_used(db) == T1


@pytest.mark.asyncio
async def test_l13_terminal_emitted_at_wins_over_envelope_time(runner):
    data = _terminal(emitted_at=T1, _envelope_timestamp=T0)
    await runner.project_event(TOPIC_COMPLETED, data, _meta(TOPIC_COMPLETED))
    writes = _writes(runner)
    assert len(writes) == 1
    assert datetime.fromisoformat(T1) in writes[0].args
    assert datetime.fromisoformat(T0) not in writes[0].args


@pytest.mark.asyncio
async def test_l14_unrelated_subscription_is_not_a_terminal(tmp_path, monkeypatch):
    contract = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text())
    extra = "onex.evt.omnimarket.unrelated-thing-happened.v1"
    contract["event_bus"]["subscribe_topics"].append(extra)
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract))
    r = HandlerTenantCredentialsProjectionRunner(contract_path=path)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    r._db = db
    monkeypatch.setattr(r, "publish_snapshot_delta", AsyncMock(return_value=True))
    assert await r.project_event(extra, _terminal(), _meta(extra)) is False
    assert _writes(r) == []
    await r.project_event(TOPIC_FAILED, _terminal(), _meta(TOPIC_FAILED))
    assert len(_writes(r)) == 1


@pytest.mark.asyncio
async def test_l15_malformed_terminal_time_goes_to_the_dlq(
    runner: HandlerTenantCredentialsProjectionRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal whose time is not an aware timestamp is routed, never dropped."""
    publish = AsyncMock()
    monkeypatch.setattr(runner, "get_publish_fn", AsyncMock(return_value=publish))
    data = _terminal(emitted_at="not-a-time")
    assert await runner.project_event(TOPIC_COMPLETED, data, _meta(TOPIC_COMPLETED))
    assert _writes(runner) == []
    topics = [call.args[0] for call in publish.await_args_list]
    assert topics == ["onex.dlq.omnimarket.tenant-credential-projection-malformed.v1"]
