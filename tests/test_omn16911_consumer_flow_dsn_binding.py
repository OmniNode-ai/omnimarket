# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16911 — the writer's own pool dials the identity the topology proved.

``ConsumerFlowProjectionWriter`` declares its two tables in the
``omninode_internal`` schema, which the runtime resolves to the
``omninode_runtime_service`` binding (principal ``omninode_runtime``, DSN env
``OMNINODE_INTERNAL_DB_URL``) and whose grants it proves before wiring. The
writer nonetheless dialled a DSN of its own: ``BaseProjectionRunner`` builds its
``AsyncpgAdapter`` from ``ModelProjectionRuntimeBinding``, whose legacy
settings fallback prefers ``OMNIDASH_ANALYTICS_DB_URL`` — the dashboard-facing
``role_omnidash`` login, which has **no USAGE** on ``omninode_internal`` and
never should: that is the whole point of a narrow dashboard role.

Live on the ``.201`` dev lane, 2026-08-28: every heartbeat carrying a window
raised ``InsufficientPrivilegeError: permission denied for schema
omninode_internal`` on ``_SELECT_UPSTREAM``, DLQ'd at ~6/min, and
``consumer_flow_windows`` held 0 rows.

The runner therefore exposes a seam the runtime binds through, and the adapter
refuses a rebind that would silently strand an already-open pool.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_consumer_flow.handlers.handler_consumer_flow_runner import (
    ConsumerFlowProjectionWriter,
)
from omnimarket.projection.runner import BaseProjectionRunner

_INTERNAL_DSN = "postgresql://omninode_runtime:pw@postgres:5432/omnidash_analytics"
_ANALYTICS_DSN = "postgresql://role_omnidash:pw@postgres:5432/omnidash_analytics"


@pytest.fixture(autouse=True)
def _legacy_settings_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce the lane's env: the analytics DSN is what construction finds."""
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", _ANALYTICS_DSN)
    monkeypatch.setenv("KAFKA_BROKERS", "localhost:9092")


def _writer() -> ConsumerFlowProjectionWriter:
    contract = (
        Path(__file__).resolve().parents[1]
        / "src/omnimarket/nodes/node_projection_consumer_flow/contract.yaml"
    )
    return ConsumerFlowProjectionWriter(contract_path=contract)


@pytest.mark.unit
def test_the_writer_starts_on_the_dashboard_dsn_the_lane_gave_it() -> None:
    """Pins the defect's precondition, so the fix below is not a tautology.

    Construction resolves ``OMNIDASH_ANALYTICS_DB_URL``. That is the role with
    no USAGE on ``omninode_internal``; nothing in the node's own construction
    path can know that, which is exactly why the runtime must bind it.
    """
    writer = _writer()
    assert writer.db.dsn == _ANALYTICS_DSN


@pytest.mark.unit
def test_declaring_in_process_dispatch_obliges_the_bind_seam() -> None:
    """The obligation pair the runtime enforces, asserted on the real class.

    A handler that declares in-process dispatch AND serves its own database is
    refused at wiring time unless it exposes ``bind_projection_database_url``.
    """
    writer = _writer()
    assert writer.onex_runtime_inprocess_dispatch is True
    assert callable(getattr(writer.db, "connect", None))
    assert callable(getattr(writer.db, "close", None))
    assert callable(getattr(writer, "bind_projection_database_url", None))


@pytest.mark.unit
def test_binding_replaces_the_settings_derived_dsn() -> None:
    """AC1/AC2: after the runtime binds, the pool dials omninode_runtime."""
    writer = _writer()
    writer.bind_projection_database_url(_INTERNAL_DSN)
    assert writer.db.dsn == _INTERNAL_DSN


@pytest.mark.unit
def test_every_projection_runner_inherits_the_seam() -> None:
    """The seam is on the base, so no sibling can drift back to its own DSN."""
    assert callable(getattr(BaseProjectionRunner, "bind_projection_database_url", None))


@pytest.mark.unit
def test_rebinding_a_connected_pool_is_refused() -> None:
    """A live pool is already dialled; swapping its DSN underneath is a lie."""
    adapter = AsyncpgAdapter(dsn=_ANALYTICS_DSN)
    adapter._pool = object()  # a connected adapter, without a live server
    with pytest.raises(RuntimeError, match="connected"):
        adapter.rebind(_INTERNAL_DSN)
    assert adapter.dsn == _ANALYTICS_DSN


@pytest.mark.unit
def test_an_empty_dsn_is_refused() -> None:
    """Fail fast: an unresolved binding must not blank out a working DSN."""
    adapter = AsyncpgAdapter(dsn=_ANALYTICS_DSN)
    with pytest.raises(ValueError, match="non-empty"):
        adapter.rebind("   ")
    assert adapter.dsn == _ANALYTICS_DSN


@pytest.mark.unit
def test_a_bound_writer_connects_to_the_bound_dsn() -> None:
    """The bind reaches the pool the message path actually opens."""
    writer = _writer()
    writer.bind_projection_database_url(_INTERNAL_DSN)

    dialled: list[str] = []

    async def _fake_create_pool(dsn: str, **_: object) -> object:
        dialled.append(dsn)
        return object()

    import omnimarket.adapters.asyncpg_adapter as adapter_module

    original = adapter_module.asyncpg.create_pool
    adapter_module.asyncpg.create_pool = _fake_create_pool  # type: ignore[assignment]
    try:
        asyncio.run(writer.db.connect())
    finally:
        adapter_module.asyncpg.create_pool = original  # type: ignore[assignment]

    assert dialled == [_INTERNAL_DSN]
