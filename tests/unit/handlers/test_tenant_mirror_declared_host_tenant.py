# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23371: the local tenant mirror admits the tenant a host declares, and no other.

A lab lane carries ``ONEX_TENANT_ID`` (the tenant its host's row of the
``lab_hosts`` overlay declares), so the in-process delegate retry resolves that
tenant and ``require_tenant_mirrored`` (OMN-22221) used to refuse it on every lab
host: the host's local sqlite mirror held only the install's own minted identity.
These tests pin the admission: a tenant the lab host table declares for THIS host
is recorded in the evidence store's mirror and the run goes on; a tenant it does
not declare for this host is refused with the same typed refusal.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml

from omnimarket.handlers.routing import delegation_backend_resolution
from omnimarket.handlers.tenant_mirror_requirement import require_tenant_mirrored
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
    TENANT_REGISTRY_MIRROR_TABLE,
    TenantRegistryResolutionError,
)

_THIS_HOST = "lab-host-a"
_OTHER_HOST = "lab-host-b"
_DECLARED_TENANT = UUID("0f1e2d3c-4b5a-4968-8776-655443322110")
_OTHER_HOST_TENANT = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")

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


@pytest.fixture(autouse=True)
def _clear_health_cache() -> None:
    handler_llm_delegation_call._health_cache.clear()


@pytest.fixture(autouse=True)
def _settings_cache_isolated() -> Iterator[None]:
    """Drop the cached settings before and after each test.

    The golden test sets ONEX_TENANT_ID and clears the settings cache so the
    run reads it; monkeypatch restores the environment at teardown but the
    cache would keep the declared tenant for every later test in the process.
    """
    from omnimarket.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def lab_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A lane on ``_THIS_HOST`` whose lab host table declares one tenant per host."""
    table = tmp_path / "lab_hosts.yaml"
    table.write_text(
        yaml.safe_dump(
            {
                "hosts": [
                    {
                        "name": _THIS_HOST,
                        "target": "this.example",
                        "tenant_id": str(_DECLARED_TENANT),
                    },
                    {
                        "name": _OTHER_HOST,
                        "target": "other.example",
                        "tenant_id": str(_OTHER_HOST_TENANT),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(table))
    monkeypatch.setenv("ONEX_LANE_HOST", _THIS_HOST)
    return _THIS_HOST


def _mirror_rows(db_path: Path, tenant_uuid: UUID) -> list[tuple[str, str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        try:
            return [
                (str(r[0]), str(r[1]))
                for r in conn.execute(
                    f"SELECT tenant_slug, tenant_uuid FROM {TENANT_REGISTRY_MIRROR_TABLE} "
                    "WHERE tenant_uuid = ?",
                    (str(tenant_uuid),),
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()


def test_declared_host_tenant_is_admitted_and_mirrored(
    tmp_path: Path, lab_host: str
) -> None:
    """The tenant this host's row declares passes, and is recorded in the mirror."""
    db_path = tmp_path / "delegation.sqlite"

    require_tenant_mirrored(SqliteDatabaseAdapter(db_path), str(_DECLARED_TENANT))

    rows = _mirror_rows(db_path, _DECLARED_TENANT)
    assert [uuid for _, uuid in rows] == [str(_DECLARED_TENANT)]


def test_declared_host_tenant_admission_is_idempotent(
    tmp_path: Path, lab_host: str
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(db_path)

    require_tenant_mirrored(adapter, str(_DECLARED_TENANT))
    require_tenant_mirrored(adapter, str(_DECLARED_TENANT))

    assert len(_mirror_rows(db_path, _DECLARED_TENANT)) == 1


def test_undeclared_tenant_is_still_refused(tmp_path: Path, lab_host: str) -> None:
    """A tenant no host row declares is refused with the OMN-22221 refusal."""
    db_path = tmp_path / "delegation.sqlite"
    unknown = str(uuid4())

    with pytest.raises(TenantRegistryResolutionError, match="OMN-22221") as excinfo:
        require_tenant_mirrored(SqliteDatabaseAdapter(db_path), unknown)

    assert unknown in str(excinfo.value)
    assert _mirror_rows(db_path, UUID(unknown)) == []


def test_undeclared_tenant_is_refused_where_the_declared_tenant_is_admitted(
    tmp_path: Path, lab_host: str
) -> None:
    """One mirror admits this host's tenant and refuses a tenant no row declares."""
    db_path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(db_path)
    unknown = str(uuid4())

    require_tenant_mirrored(adapter, str(_DECLARED_TENANT))
    with pytest.raises(TenantRegistryResolutionError, match="OMN-22221"):
        require_tenant_mirrored(adapter, unknown)

    assert len(_mirror_rows(db_path, _DECLARED_TENANT)) == 1
    assert _mirror_rows(db_path, UUID(unknown)) == []


def test_tenant_declared_for_another_host_is_refused(
    tmp_path: Path, lab_host: str
) -> None:
    """The declaration is per host: another host's tenant is not admitted here."""
    db_path = tmp_path / "delegation.sqlite"

    with pytest.raises(TenantRegistryResolutionError, match="OMN-22221"):
        require_tenant_mirrored(SqliteDatabaseAdapter(db_path), str(_OTHER_HOST_TENANT))

    assert _mirror_rows(db_path, _OTHER_HOST_TENANT) == []


def test_declared_tenant_is_refused_when_the_lane_names_no_host(
    tmp_path: Path, lab_host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no ``ONEX_LANE_HOST`` there is no host row to read: the refusal stands."""
    monkeypatch.delenv("ONEX_LANE_HOST")
    db_path = tmp_path / "delegation.sqlite"

    with pytest.raises(TenantRegistryResolutionError, match="OMN-22221"):
        require_tenant_mirrored(SqliteDatabaseAdapter(db_path), str(_DECLARED_TENANT))


def test_declared_tenant_is_refused_when_the_host_has_no_row(
    tmp_path: Path, lab_host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_LANE_HOST", "lab-host-not-in-the-table")
    db_path = tmp_path / "delegation.sqlite"

    with pytest.raises(TenantRegistryResolutionError, match="OMN-22221"):
        require_tenant_mirrored(SqliteDatabaseAdapter(db_path), str(_DECLARED_TENANT))


def test_declared_tenant_is_not_admitted_into_a_non_sqlite_target(
    lab_host: str,
) -> None:
    """Only a local sqlite evidence file is written; a deployed store is never seeded."""

    class _DeployedStore:
        def __init__(self) -> None:
            self.writes: list[str] = []

        def query(self, table: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
            return []

        def upsert(self, *args: Any, **kwargs: Any) -> None:
            self.writes.append("upsert")

    store = _DeployedStore()
    with pytest.raises(TenantRegistryResolutionError, match="OMN-22221"):
        require_tenant_mirrored(store, str(_DECLARED_TENANT))
    assert store.writes == []


def _passing_named_test_runner(
    request: ModelNamedTestRunRequest,
) -> ModelNamedTestRunResult:
    return ModelNamedTestRunResult(
        status=EnumNamedTestRunStatus.COMPLETED,
        run=ModelNamedTestRun(test_ids=request.spec.test_ids, exit_code=0),
    )


def _patch_backends_and_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        delegation_backend_resolution, "load_bifrost_backends", lambda **_: _BACKENDS
    )
    monkeypatch.setattr(transport, "probe_health", lambda *_args, **_kwargs: True)

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
                    "prompt_tokens": 11,
                    "completion_tokens": 22,
                    "total_tokens": 33,
                },
            },
            latency_ms=42,
        )

    monkeypatch.setattr(transport, "post_chat_completion", fake_post)


def _dispatch(
    port: LocalDelegationDispatchPort, tmp_path: Path, *, tenant_id: str, cid: UUID
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
            correlation_id=cid,
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


def test_lab_lane_in_process_delegate_with_declared_tenant_completes_with_its_row(
    tmp_path: Path, lab_host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Golden: the lane's tenant (ONEX_TENANT_ID) is the host's declared one; the run completes."""
    monkeypatch.setenv("ONEX_TENANT_ID", str(_DECLARED_TENANT))
    from omnimarket.config.settings import get_settings

    get_settings.cache_clear()
    db_path = tmp_path / "delegation.sqlite"
    _patch_backends_and_transport(monkeypatch)
    port = LocalDelegationDispatchPort(
        named_test_runner=_passing_named_test_runner,
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )
    cid = uuid4()

    result = _dispatch(port, tmp_path, tenant_id=str(_DECLARED_TENANT), cid=cid)

    assert result["status"] == "completed"
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT tenant_id FROM delegation_events WHERE correlation_id = ?",
            (str(cid),),
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == [str(_DECLARED_TENANT)]


def test_lab_lane_in_process_delegate_with_undeclared_tenant_is_refused_before_any_row(
    tmp_path: Path, lab_host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error: an undeclared tenant is refused at the start with no evidence row."""
    db_path = tmp_path / "delegation.sqlite"
    _patch_backends_and_transport(monkeypatch)
    port = LocalDelegationDispatchPort(
        named_test_runner=_passing_named_test_runner,
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )
    unknown = str(uuid4())

    with pytest.raises(TenantRegistryResolutionError, match="OMN-22221"):
        _dispatch(port, tmp_path, tenant_id=unknown, cid=uuid4())

    conn = sqlite3.connect(str(db_path))
    try:
        try:
            count = conn.execute("SELECT count(*) FROM delegation_events").fetchone()[0]
        except sqlite3.OperationalError:
            count = 0
    finally:
        conn.close()
    assert count == 0
