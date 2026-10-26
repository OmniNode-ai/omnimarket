# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21305 AC3, the served half: the refused run reaches the Runs page's feed.

The local dashboard's Runs page lists ``status=failed`` from the rows the
projection read node serves on ``delegation.decisions.v1`` (decision D1 (a)):
a row is failed when ``terminal_ok`` is 0, and its cause is
``terminal_failure_cause``. A keyless run on a fresh HOME is refused before any
rung runs; before the fix it wrote no row, so the read node served nothing for
its correlation id and the Runs page had no failed run to show.

This test refuses that run through the local dispatch port, then reads the same
store through the read node with the shipped exposure map, the way the
dashboard does, and requires exactly one served row for the run's correlation
id and the install's tenant, failed, with its typed cause. The page itself
(filtering and rendering that row) is the dashboard's and is not tested here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from tests.unit.nodes.node_delegate_skill_orchestrator.test_routing_refusal_publishes_a_terminal_omn19919 import (
    _declare,
    _dispatch,
    _install_tenant,
    _port,
    _RecordingEffect,
    clean_install,
)

pytestmark = pytest.mark.unit

__all__ = ["clean_install"]

_OVERLAY_ENV = "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY"
_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"


def _bind_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db: Path) -> None:
    overlay = tmp_path / "projection_binding.yaml"
    overlay.write_text(
        f"kafka_bootstrap_servers: inmemory\ndatabase_url: 'sqlite:///{db}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(_OVERLAY_ENV, str(overlay))


def _served(tenant: str, correlation_id: str) -> object:
    async def read() -> object:
        handler = HandlerProjectionRead()
        try:
            return await handler.handle(
                ModelProjectionReadRequest(
                    topic=_DECISIONS,
                    tenant_id=tenant,
                    row_correlation_id=correlation_id,
                )
            )
        finally:
            await handler.close()

    return asyncio.run(read())


def test_ac3_the_refused_run_is_served_as_one_failed_row_with_its_cause(
    clean_install: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(clean_install)
    correlation_id = uuid4()
    with pytest.raises(ProtocolConfigurationError):
        _dispatch(_port(tmp_path, _RecordingEffect()), correlation_id)

    _bind_store(monkeypatch, tmp_path, tmp_path / "evidence.sqlite")
    result = _served(_install_tenant(), str(correlation_id))

    assert result.ok is True, result
    assert len(result.rows) == 1, result.rows
    (row,) = result.rows
    assert row["correlation_id"] == str(correlation_id)
    assert row["tenant_id"] == _install_tenant()
    assert row["terminal_ok"] in (0, False)
    assert row["terminal_failure_cause"] == "refused_before_dispatch"
