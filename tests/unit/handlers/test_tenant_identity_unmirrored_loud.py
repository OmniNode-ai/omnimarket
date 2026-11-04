# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22221: a delegation whose tenant has no mirror row is refused loudly.

An in-process delegation from a remote lane resolves its tenant from the lane's
environment. When that tenant is not a row of the evidence store's
``tenant_registry_mirror``, the run used to go on: the provider was called, the
run steps were written, the response completed, and the only trace of the
missing ``delegation_events`` row was a WARNING from the best-effort evidence
write. These tests pin the replacement: the run is refused at its start, before
any provider call and before any row, with an error that names the tenant.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.handlers.routing import delegation_backend_resolution
from omnimarket.local_deployment.tenant_identity import (
    _ensure_tables_on,
    _write_mirror_row,
)
from omnimarket.models.delegation.wire.model_named_test_run import ModelNamedTestRun
from omnimarket.models.delegation.wire.model_named_test_run_request import (
    EnumNamedTestRunStatus,
    ModelNamedTestRunRequest,
    ModelNamedTestRunResult,
    ModelNamedTestSpec,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call,
    transport,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.tenant_registry_resolution import (
    TenantRegistryResolutionError,
)

_BACKENDS: list[dict[str, object]] = [
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

#: A slug the closed legacy map does not hold and the mirror under test lacks.
_UNMIRRORED_SLUG = "remote-lane-tenant-not-in-mirror"


def _passing_named_test_runner(
    request: ModelNamedTestRunRequest,
) -> ModelNamedTestRunResult:
    return ModelNamedTestRunResult(
        status=EnumNamedTestRunStatus.COMPLETED,
        run=ModelNamedTestRun(test_ids=request.spec.test_ids, exit_code=0),
    )


@pytest.fixture(autouse=True)
def _clear_health_cache() -> None:
    handler_llm_delegation_call._health_cache.clear()


class _ProviderCalls:
    """Counts the provider calls a test's transport stub received."""

    def __init__(self) -> None:
        self.posts = 0


def _patch_backends_and_transport(monkeypatch: pytest.MonkeyPatch) -> _ProviderCalls:
    calls = _ProviderCalls()
    monkeypatch.setattr(
        delegation_backend_resolution, "load_bifrost_backends", lambda **_: _BACKENDS
    )

    def fake_probe_health(endpoint_url: str, **_: Any) -> bool:
        return True

    def fake_post(**_: Any) -> transport.ModelTransportResponse:
        calls.posts += 1
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
                    "prompt_tokens": 11,
                    "completion_tokens": 22,
                    "total_tokens": 33,
                },
            },
            latency_ms=42,
        )

    monkeypatch.setattr(transport, "probe_health", fake_probe_health)
    monkeypatch.setattr(transport, "post_chat_completion", fake_post)
    return calls


def _dispatch(
    port: LocalDelegationDispatchPort,
    tmp_path: Path,
    *,
    correlation_id: UUID,
    tenant_id: str | None,
) -> dict[str, object]:
    return asyncio.run(
        port.dispatch(
            named_tests=ModelNamedTestSpec(
                workspace_root=str(tmp_path),
                answer_path="reverse_impl.py",
                test_ids=("test_reverse.py::test_reverse",),
            ),
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
            tenant_id=tenant_id,
        )
    )


def _count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        try:
            return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()


def _mirror(db_path: Path, *, slug: str, tenant_uuid: UUID) -> None:
    """Record one tenant in the evidence store's own ``tenant_registry_mirror``."""
    from datetime import UTC, datetime

    adapter = SqliteDatabaseAdapter(db_path)
    _ensure_tables_on(adapter)
    _write_mirror_row(
        adapter, tenant_uuid=tenant_uuid, tenant_slug=slug, now=datetime.now(tz=UTC)
    )


def test_unmirrored_refused_slug_names_tenant_before_any_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slug with no mirror row: refused at the start, naming the tenant."""
    db_path = tmp_path / "delegation.sqlite"
    calls = _patch_backends_and_transport(monkeypatch)
    port = LocalDelegationDispatchPort(
        named_test_runner=_passing_named_test_runner,
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )

    with pytest.raises(TenantRegistryResolutionError) as excinfo:
        _dispatch(port, tmp_path, correlation_id=uuid4(), tenant_id=_UNMIRRORED_SLUG)

    assert _UNMIRRORED_SLUG in str(excinfo.value)
    assert "OMN-22221" in str(excinfo.value)
    assert calls.posts == 0, "the provider must not be called for a refused tenant"
    assert _count(db_path, "delegation_events") == 0
    assert _count(db_path, "delegation_run_steps") == 0


def test_unmirrored_refused_uuid_names_tenant_before_any_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway's UUID form with no mirror row is refused the same way."""
    db_path = tmp_path / "delegation.sqlite"
    calls = _patch_backends_and_transport(monkeypatch)
    port = LocalDelegationDispatchPort(
        named_test_runner=_passing_named_test_runner,
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )
    tenant_uuid = str(uuid4())

    with pytest.raises(TenantRegistryResolutionError) as excinfo:
        _dispatch(port, tmp_path, correlation_id=uuid4(), tenant_id=tenant_uuid)

    assert tenant_uuid in str(excinfo.value)
    assert calls.posts == 0
    assert _count(db_path, "delegation_events") == 0


def test_unmirrored_refused_when_mirror_holds_only_other_tenants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A populated mirror that lacks THIS tenant refuses; it is not a blanket pass."""
    db_path = tmp_path / "delegation.sqlite"
    _mirror(db_path, slug="some-other-tenant", tenant_uuid=uuid4())
    calls = _patch_backends_and_transport(monkeypatch)
    port = LocalDelegationDispatchPort(
        named_test_runner=_passing_named_test_runner,
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )

    with pytest.raises(TenantRegistryResolutionError, match=_UNMIRRORED_SLUG):
        _dispatch(port, tmp_path, correlation_id=uuid4(), tenant_id=_UNMIRRORED_SLUG)

    assert calls.posts == 0


def test_remote_lane_tenant_mirrored_completed_delegation_writes_its_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: the same run with the tenant mirrored completes with a row.

    This is the part of the remote-lane contract omnimarket owns: a tenant that
    IS a row of the mirror the evidence is written to leaves its
    ``delegation_events`` row. That the tenant a remote lane exports on h201 is
    such a row is a fact about that host's mirror, not about this code.
    """
    db_path = tmp_path / "delegation.sqlite"
    tenant_uuid = uuid4()
    _mirror(db_path, slug="remote-lane-mirrored", tenant_uuid=tenant_uuid)
    calls = _patch_backends_and_transport(monkeypatch)
    port = LocalDelegationDispatchPort(
        named_test_runner=_passing_named_test_runner,
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )
    correlation_id = uuid4()

    result = _dispatch(
        port, tmp_path, correlation_id=correlation_id, tenant_id=str(tenant_uuid)
    )

    assert result["status"] == "completed"
    assert calls.posts == 1
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT tenant_id FROM delegation_events WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == [str(tenant_uuid)]


def test_remote_lane_tenant_mirrored_by_slug_is_accepted(
    tmp_path: Path,
) -> None:
    """The check passes for a slug the mirror holds, and returns without raising."""
    from omnimarket.handlers.tenant_mirror_requirement import require_tenant_mirrored

    db_path = tmp_path / "delegation.sqlite"
    _mirror(db_path, slug="remote-lane-mirrored", tenant_uuid=uuid4())
    require_tenant_mirrored(SqliteDatabaseAdapter(db_path), "remote-lane-mirrored")


def test_unmirrored_refused_check_without_a_reader_does_not_refuse(
    tmp_path: Path,
) -> None:
    """A target that cannot be read is not a mirror that lacks the tenant."""
    from omnimarket.handlers.tenant_mirror_requirement import require_tenant_mirrored

    require_tenant_mirrored(object(), _UNMIRRORED_SLUG)
