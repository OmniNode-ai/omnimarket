# SPDX-License-Identifier: MIT
"""Unit tests for HandlerDemoRehearsal.

All network I/O is patched. Verifies dry_run, evidence artifact writing,
status classification, and failure recording.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from omnimarket.nodes.node_demo_rehearsal.handlers.handler_demo_rehearsal import (
    HandlerDemoRehearsal,
    ModelDemoRehearsalRequest,
)


@pytest.fixture
def tmp_omni_home(tmp_path: Path) -> Path:
    return tmp_path


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rehearsal_dry_run_no_artifact(tmp_omni_home: Path) -> None:
    handler = HandlerDemoRehearsal()
    with (
        patch.object(
            handler, "_probe_topology", new=AsyncMock(return_value={"nodes": 3})
        ),
        patch.object(
            handler, "_probe_projection", new=AsyncMock(return_value={"id": "r1"})
        ),
        patch.object(
            handler,
            "_probe_dashboard_api",
            new=AsyncMock(return_value={"status": "ok"}),
        ),
    ):
        result = await handler.handle(
            ModelDemoRehearsalRequest(
                run_id="test-run-dry",
                omni_home=str(tmp_omni_home),
                dry_run=True,
            )
        )

    assert result.overall_status == "GREEN"
    assert result.failure_count == 0
    assert result.dry_run is True
    bundle_path = (
        tmp_omni_home
        / "docs/evidence/demo-readiness/test-run-dry/rehearsal_bundle.json"
    )
    assert not bundle_path.exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rehearsal_writes_artifact(tmp_omni_home: Path) -> None:
    handler = HandlerDemoRehearsal()
    with (
        patch.object(
            handler, "_probe_topology", new=AsyncMock(return_value={"nodes": 2})
        ),
        patch.object(handler, "_probe_projection", new=AsyncMock(return_value=None)),
        patch.object(
            handler,
            "_probe_dashboard_api",
            new=AsyncMock(return_value={"status": "ok"}),
        ),
    ):
        result = await handler.handle(
            ModelDemoRehearsalRequest(
                run_id="test-run-write",
                omni_home=str(tmp_omni_home),
                dry_run=False,
            )
        )

    assert result.overall_status == "DEGRADED"
    bundle_path = Path(result.bundle_path)
    assert bundle_path.exists()
    data = json.loads(bundle_path.read_text())
    assert data["overall_status"] == "DEGRADED"
    assert data["rehearsal_id"] is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rehearsal_broken_when_topology_fails(tmp_omni_home: Path) -> None:
    handler = HandlerDemoRehearsal()
    with (
        patch.object(handler, "_probe_topology", new=AsyncMock(return_value={})),
        patch.object(handler, "_probe_projection", new=AsyncMock(return_value=None)),
        patch.object(handler, "_probe_dashboard_api", new=AsyncMock(return_value=None)),
    ):
        result = await handler.handle(
            ModelDemoRehearsalRequest(
                run_id="test-run-broken",
                omni_home=str(tmp_omni_home),
                dry_run=True,
            )
        )

    assert result.overall_status == "BROKEN"
    assert result.failure_count >= 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rehearsal_degraded_when_dashboard_fails(tmp_omni_home: Path) -> None:
    handler = HandlerDemoRehearsal()
    with (
        patch.object(
            handler, "_probe_topology", new=AsyncMock(return_value={"nodes": 1})
        ),
        patch.object(handler, "_probe_projection", new=AsyncMock(return_value=None)),
        patch.object(handler, "_probe_dashboard_api", new=AsyncMock(return_value=None)),
    ):
        result = await handler.handle(
            ModelDemoRehearsalRequest(
                run_id="test-run-degraded",
                omni_home=str(tmp_omni_home),
                dry_run=True,
            )
        )

    assert result.overall_status == "DEGRADED"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rehearsal_degraded_when_projection_missing(
    tmp_omni_home: Path,
) -> None:
    handler = HandlerDemoRehearsal()
    with (
        patch.object(
            handler, "_probe_topology", new=AsyncMock(return_value={"nodes": 1})
        ),
        patch.object(handler, "_probe_projection", new=AsyncMock(return_value=None)),
        patch.object(
            handler,
            "_probe_dashboard_api",
            new=AsyncMock(return_value={"status": "ok"}),
        ),
    ):
        result = await handler.handle(
            ModelDemoRehearsalRequest(
                run_id="test-run-projection-missing",
                omni_home=str(tmp_omni_home),
                dry_run=True,
            )
        )

    assert result.overall_status == "DEGRADED"
    assert result.failure_count == 1
    assert result.rehearsal_bundle.failures[0]["dimension"] == "projection"
