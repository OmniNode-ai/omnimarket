# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18699: the local dispatch port refuses an un-initialised install.

Before this ticket the port ended its tenant precedence chain at ``or None`` and
the evidence writer substituted ``HOUSE_TENANT_SLUG``, so every local row on an
install that had never resolved a tenant was recorded as OmniNode's. These tests
drive the real ``LocalDelegationDispatchPort`` against a tmp_path store and
assert the two halves the ruling asks for: a run with no identity REFUSES and
does no provider work, and a run with a minted identity records under it.

The transport is monkeypatched at the effect boundary, so no network call is
made and the provider-call counter below is a real observation of whether the
refusal preceded the work.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.local_deployment import tenant_identity
from omnimarket.local_deployment.tenant_identity import (
    LocalTenantIdentityError,
    mint_local_tenant_identity,
    reset_local_tenant_identity_cache,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call,
    transport,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG, HOUSE_TENANT_UUID
from omnimarket.routing import delegation_backend_resolution
from tests.fixtures.judge_inference import CannedAdequacyBridge

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_caches() -> None:
    handler_llm_delegation_call._health_cache.clear()
    reset_local_tenant_identity_cache()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "delegation.sqlite"


@pytest.fixture
def uninitialised_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the INSTALL identity store at an empty tmp file.

    tests/conftest.py mints a session identity so the rest of the suite is an
    initialised install; these tests are specifically about the install that is
    not, so they redirect the same seam the fixture does.
    """
    store = tmp_path / "install" / "delegation.sqlite"
    monkeypatch.setattr(
        tenant_identity, "default_evidence_db_path", lambda: store, raising=True
    )
    tenant_identity.reset_local_tenant_identity_cache()
    return store


@pytest.fixture
def fake_backends(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    backends: list[dict[str, object]] = [
        {
            "backend_id": "local-coder",
            "endpoint_url": "http://inference.example:8000/v1/chat/completions",
            "model_name": "Qwen3.6-35B-A3B",
            "tier": "local",
            "max_tokens": 4096,
            "timeout_ms": 300000,
            "capabilities": ["code_generation"],
        }
    ]
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: backends,
    )
    return backends


@pytest.fixture
def provider_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count real provider invocations so "refused first" is an observation."""
    calls: list[str] = []

    def _fake_probe_health(endpoint_url: str, **_: Any) -> bool:
        return True

    def _fake_post(
        *,
        endpoint_url: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        extra_headers: dict[str, str] | None = None,
        runtime_profile: str | None = None,
    ) -> transport.ModelTransportResponse:
        calls.append(endpoint_url)
        return transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "choices": [{"message": {"content": "def reverse(s): return s[::-1]"}}],
                "model": "Qwen3.6-35B-A3B",
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 22,
                    "total_tokens": 33,
                },
            },
            latency_ms=42,
        )

    monkeypatch.setattr(transport, "probe_health", _fake_probe_health)
    monkeypatch.setattr(transport, "post_chat_completion", _fake_post)
    return calls


def _port(db_path: Path) -> LocalDelegationDispatchPort:
    return LocalDelegationDispatchPort(
        evidence_db_path=db_path,
        effect_process_boundary=False,
        judge=HandlerJudgeAdequacy(
            inference_bridge=CannedAdequacyBridge(adequacy_score=0.95)
        ),
    )


def _dispatch(port: LocalDelegationDispatchPort, correlation_id: UUID) -> Any:
    return asyncio.run(
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


def test_uninitialised_install_refuses_before_any_provider_call(
    db_path: Path,
    uninitialised_install: Path,
    fake_backends: list[dict[str, object]],
    provider_calls: list[str],
) -> None:
    """AC2: a run with no identity fails fast, naming what is missing."""
    with pytest.raises(LocalTenantIdentityError) as excinfo:
        _dispatch(_port(db_path), uuid4())

    assert "onex local init" in str(excinfo.value)
    # The refusal preceded the work. A refusal that arrives after the tokens
    # are spent is a different, worse thing.
    assert provider_calls == []
    # And nothing was recorded under the house tenant on the way out.
    assert not db_path.exists() or _tenants_in(db_path) == set()


def test_uninitialised_install_never_writes_a_house_tenant_row(
    db_path: Path,
    uninitialised_install: Path,
    fake_backends: list[dict[str, object]],
    provider_calls: list[str],
) -> None:
    """AC1 falsifier, stated as its own assertion: no house-tenant row appears."""
    with pytest.raises(LocalTenantIdentityError):
        _dispatch(_port(db_path), uuid4())
    recorded = _tenants_in(db_path)
    assert str(HOUSE_TENANT_UUID) not in recorded
    assert HOUSE_TENANT_SLUG not in recorded


def test_initialised_install_records_under_its_own_minted_identity(
    db_path: Path,
    uninitialised_install: Path,
    fake_backends: list[dict[str, object]],
    provider_calls: list[str],
) -> None:
    """AC1/AC3: the row carries the minted identity, and no cloud identifier."""
    minted = mint_local_tenant_identity(db_path=uninitialised_install)
    reset_local_tenant_identity_cache()

    correlation_id = uuid4()
    result = _dispatch(_port(db_path), correlation_id)

    assert provider_calls, "the provider should have been reached once initialised"
    assert result["status"] in {"completed", "failed"}

    rows = _rows_for(db_path, correlation_id)
    assert len(rows) == 1, f"expected one evidence row, got {rows}"
    assert rows[0]["tenant_id"] == str(minted.tenant_uuid)
    assert rows[0]["tenant_id"] != str(HOUSE_TENANT_UUID)


def _tenants_in(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(str(db_path))
    try:
        names = {r[1] for r in conn.execute("PRAGMA table_info(delegation_events)")}
        if "tenant_id" not in names:
            return set()
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT tenant_id FROM delegation_events"
            ).fetchall()
            if row[0] is not None
        }
    finally:
        conn.close()


def _rows_for(db_path: Path, correlation_id: UUID) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM delegation_events WHERE correlation_id = ?",
                (str(correlation_id),),
            ).fetchall()
        ]
    finally:
        conn.close()
