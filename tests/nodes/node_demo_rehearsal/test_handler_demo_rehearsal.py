# SPDX-License-Identifier: MIT
"""Unit tests for HandlerDemoRehearsal.

All network I/O is patched. Verifies dry_run, evidence artifact writing,
status classification, and failure recording.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import yaml

from omnimarket.nodes.node_demo_rehearsal.handlers.handler_demo_rehearsal import (
    HandlerDemoRehearsal,
    ModelDemoRehearsalRequest,
)


@pytest.fixture
def tmp_omni_home(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture(autouse=True)
def configured_dashboard_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_DASHBOARD_URL", "https://dashboard.example.test")


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
    assert result.model_dump(mode="json")["node_id"] == "demo_rehearsal"
    assert result.run_id == "test-run-dry"
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
    assert result.dashboard_configuration == "CONFIGURED"
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


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("dashboard_url", [None, "", "   "])
async def test_unconfigured_dashboard_url_is_broken_without_http_probe(
    tmp_omni_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    dashboard_url: str | None,
) -> None:
    if dashboard_url is None:
        monkeypatch.delenv("DEMO_DASHBOARD_URL", raising=False)
    else:
        monkeypatch.setenv("DEMO_DASHBOARD_URL", dashboard_url)
    handler = HandlerDemoRehearsal()
    with (
        patch(
            "httpx.AsyncClient"
        ) as client,  # onex-allow-faked-boundary: assert unconfigured dashboard never constructs an HTTP client
        patch.object(handler, "_probe_projection", new=AsyncMock(return_value=None)),
    ):
        result = await handler.handle(
            ModelDemoRehearsalRequest(
                run_id="unconfigured-dashboard",
                omni_home=str(tmp_omni_home),
                dry_run=True,
            )
        )

    client.assert_not_called()
    assert result.overall_status == "BROKEN"
    assert result.dashboard_configuration == "UNCONFIGURED"
    assert result.model_dump(mode="json")["node_id"] == "demo_rehearsal"
    assert result.model_dump(mode="json")["dashboard_configuration"] == "UNCONFIGURED"
    assert any(
        failure["dimension"] == "dashboard"
        and failure["severity"] == "critical"
        and "DEMO_DASHBOARD_URL" in failure["msg"]
        and "unconfigured" in failure["msg"]
        for failure in result.rehearsal_bundle.failures
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_configured_dashboard_probes_resolved_endpoint() -> None:
    handler = HandlerDemoRehearsal()
    requested_urls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, json={"status": "ok"})

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    with patch(
        "httpx.AsyncClient",
        side_effect=lambda **kwargs: real_client(transport=transport, **kwargs),
    ):  # onex-allow-faked-boundary: local HTTP transport proves configured probe URL without external egress
        assert await handler._probe_topology() == {"status": "ok"}
        assert await handler._probe_dashboard_api() == {"status": "ok"}

    assert requested_urls == [
        "https://dashboard.example.test/api/topology",
        "https://dashboard.example.test/api/health",
    ]


@pytest.mark.unit
def test_rehearsal_terminal_topic_is_declared_for_publishing() -> None:
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_demo_rehearsal/contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == "onex.evt.omnimarket.demo-rehearsed.v1"
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
