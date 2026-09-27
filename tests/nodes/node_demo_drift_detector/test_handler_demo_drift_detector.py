# SPDX-License-Identifier: MIT
"""Unit tests for HandlerDemoDriftDetector.

Probes are patched. Verifies finding classification, criticality assignment,
dry_run behaviour, and artifact writing.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import yaml

from omnimarket.events.demo_readiness import (
    EnumDemoCriticality,
    ModelRehearsalBundle,
)
from omnimarket.nodes.node_demo_drift_detector.handlers.handler_demo_drift_detector import (
    HandlerDemoDriftDetector,
    ModelDemoDriftDetectRequest,
)


@pytest.fixture(autouse=True)
def configured_dashboard_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_DASHBOARD_URL", "https://dashboard.example.test")


def _write_green_bundle(
    tmp_path: Path,
    topology: dict,
    dashboard: dict | None,
    projection: dict | None = None,
) -> Path:
    from datetime import UTC, datetime

    bundle = ModelRehearsalBundle(
        rehearsal_id="green-001",
        timestamp_utc=datetime.now(UTC),
        runtime_topology_manifest=topology,
        projection_row=projection,
        dashboard_api_response=dashboard,
        overall_status="GREEN",
        failures=[],
    )
    bundle_dir = tmp_path / "green"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = bundle_dir / "rehearsal_bundle.json"
    bundle_path.write_text(bundle.model_dump_json(), encoding="utf-8")
    return bundle_path


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_drift_when_identical(tmp_path: Path) -> None:
    topology = {"nodes": 3}
    dashboard = {"status": "ok"}
    green_path = _write_green_bundle(tmp_path, topology, dashboard)

    handler = HandlerDemoDriftDetector()
    with (
        patch.object(
            handler, "_probe_current_topology", new=AsyncMock(return_value=topology)
        ),
        patch.object(
            handler, "_probe_current_dashboard", new=AsyncMock(return_value=dashboard)
        ),
        patch.object(
            handler, "_probe_current_projection", new=AsyncMock(return_value=None)
        ),
    ):
        result = await handler.handle(
            ModelDemoDriftDetectRequest(
                run_id="drift-test-clean",
                proof_of_green_path=str(green_path),
                omni_home=str(tmp_path),
                dry_run=True,
            )
        )

    assert result.total_finding_count == 0
    assert result.model_dump(mode="json")["node_id"] == "demo_drift_detector"
    assert result.demo_blocker_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_demo_blocker_when_topology_gone(tmp_path: Path) -> None:
    green_path = _write_green_bundle(tmp_path, {"nodes": 3}, {"status": "ok"})

    handler = HandlerDemoDriftDetector()
    with (
        patch.object(
            handler, "_probe_current_topology", new=AsyncMock(return_value={})
        ),
        patch.object(
            handler,
            "_probe_current_dashboard",
            new=AsyncMock(return_value={"status": "ok"}),
        ),
        patch.object(
            handler, "_probe_current_projection", new=AsyncMock(return_value=None)
        ),
    ):
        result = await handler.handle(
            ModelDemoDriftDetectRequest(
                run_id="drift-test-blocker",
                proof_of_green_path=str(green_path),
                omni_home=str(tmp_path),
                dry_run=True,
            )
        )

    assert result.demo_blocker_count >= 1
    assert result.dashboard_configuration == "CONFIGURED"
    blockers = [
        f
        for f in result.drift_report.findings
        if f.criticality == EnumDemoCriticality.DEMO_BLOCKER
    ]
    assert len(blockers) >= 1
    assert not blockers[0].auto_fixable


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cosmetic_when_dashboard_differs(tmp_path: Path) -> None:
    green_path = _write_green_bundle(tmp_path, {"nodes": 1}, {"status": "ok", "v": 1})

    handler = HandlerDemoDriftDetector()
    with (
        patch.object(
            handler, "_probe_current_topology", new=AsyncMock(return_value={"nodes": 1})
        ),
        patch.object(
            handler,
            "_probe_current_dashboard",
            new=AsyncMock(return_value={"status": "ok", "v": 2}),
        ),
        patch.object(
            handler, "_probe_current_projection", new=AsyncMock(return_value=None)
        ),
    ):
        result = await handler.handle(
            ModelDemoDriftDetectRequest(
                run_id="drift-test-cosmetic",
                proof_of_green_path=str(green_path),
                omni_home=str(tmp_path),
                dry_run=True,
            )
        )

    cosmetic = [
        f
        for f in result.drift_report.findings
        if f.criticality == EnumDemoCriticality.COSMETIC
    ]
    assert len(cosmetic) >= 1
    assert cosmetic[0].auto_fixable is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_projection_drift_is_reported(tmp_path: Path) -> None:
    green_path = _write_green_bundle(
        tmp_path,
        {"nodes": 1},
        {"status": "ok"},
        projection={"id": "green"},
    )

    handler = HandlerDemoDriftDetector()
    with (
        patch.object(
            handler, "_probe_current_topology", new=AsyncMock(return_value={"nodes": 1})
        ),
        patch.object(
            handler,
            "_probe_current_dashboard",
            new=AsyncMock(return_value={"status": "ok"}),
        ),
        patch.object(
            handler,
            "_probe_current_projection",
            new=AsyncMock(return_value={"id": "current"}),
        ),
    ):
        result = await handler.handle(
            ModelDemoDriftDetectRequest(
                run_id="drift-test-projection",
                proof_of_green_path=str(green_path),
                omni_home=str(tmp_path),
                dry_run=True,
            )
        )

    projection_findings = [
        f for f in result.drift_report.findings if f.dimension == "projection"
    ]
    assert len(projection_findings) == 1
    assert projection_findings[0].criticality == EnumDemoCriticality.DEMO_DEGRADED


@pytest.mark.unit
@pytest.mark.asyncio
async def test_drift_report_written(tmp_path: Path) -> None:
    green_path = _write_green_bundle(tmp_path, {"nodes": 2}, {"status": "ok"})

    handler = HandlerDemoDriftDetector()
    with (
        patch.object(
            handler, "_probe_current_topology", new=AsyncMock(return_value={})
        ),
        patch.object(
            handler,
            "_probe_current_dashboard",
            new=AsyncMock(return_value={"status": "ok"}),
        ),
        patch.object(
            handler, "_probe_current_projection", new=AsyncMock(return_value=None)
        ),
    ):
        result = await handler.handle(
            ModelDemoDriftDetectRequest(
                run_id="drift-test-write",
                proof_of_green_path=str(green_path),
                omni_home=str(tmp_path),
                dry_run=False,
            )
        )

    report_path = Path(result.report_path)
    assert report_path.exists()
    data = json.loads(report_path.read_text())
    assert data["run_id"] == "drift-test-write"
    assert isinstance(data["findings"], list)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("dashboard_url", [None, "", "   "])
async def test_unconfigured_dashboard_url_is_blocker_without_http_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dashboard_url: str | None,
) -> None:
    if dashboard_url is None:
        monkeypatch.delenv("DEMO_DASHBOARD_URL", raising=False)
    else:
        monkeypatch.setenv("DEMO_DASHBOARD_URL", dashboard_url)
    green_path = _write_green_bundle(tmp_path, {"nodes": 2}, {"status": "ok"})
    handler = HandlerDemoDriftDetector()
    with (
        patch(
            "httpx.AsyncClient"
        ) as client,  # onex-allow-faked-boundary: assert unconfigured dashboard never constructs an HTTP client
        patch.object(
            handler, "_probe_current_projection", new=AsyncMock(return_value=None)
        ),
    ):
        result = await handler.handle(
            ModelDemoDriftDetectRequest(
                run_id="unconfigured-dashboard",
                proof_of_green_path=str(green_path),
                omni_home=str(tmp_path),
                dry_run=True,
            )
        )

    client.assert_not_called()
    assert result.demo_blocker_count >= 1
    assert result.dashboard_configuration == "UNCONFIGURED"
    assert result.model_dump(mode="json")["node_id"] == "demo_drift_detector"
    assert result.model_dump(mode="json")["dashboard_configuration"] == "UNCONFIGURED"
    assert any(
        finding.dimension == "dashboard"
        and finding.criticality == EnumDemoCriticality.DEMO_BLOCKER
        and "DEMO_DASHBOARD_URL" in finding.summary
        and "unconfigured" in finding.summary
        for finding in result.drift_report.findings
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unreachable_current_probes_block_even_with_empty_green_baseline(
    tmp_path: Path,
) -> None:
    green_path = _write_green_bundle(tmp_path, {}, None)
    handler = HandlerDemoDriftDetector()
    with (
        patch.object(
            handler, "_probe_current_topology", new=AsyncMock(return_value={})
        ),
        patch.object(
            handler, "_probe_current_dashboard", new=AsyncMock(return_value=None)
        ),
        patch.object(
            handler, "_probe_current_projection", new=AsyncMock(return_value=None)
        ),
    ):
        result = await handler.handle(
            ModelDemoDriftDetectRequest(
                run_id="empty-green-baseline",
                proof_of_green_path=str(green_path),
                omni_home=str(tmp_path),
                dry_run=True,
            )
        )

    assert result.dashboard_configuration == "CONFIGURED"
    assert result.demo_blocker_count == 2
    assert {finding.dimension for finding in result.drift_report.findings} == {
        "topology",
        "dashboard",
    }
    assert all(
        finding.criticality == EnumDemoCriticality.DEMO_BLOCKER
        for finding in result.drift_report.findings
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_configured_dashboard_probes_resolved_endpoint() -> None:
    handler = HandlerDemoDriftDetector()
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
        assert await handler._probe_current_topology() == {"status": "ok"}
        assert await handler._probe_current_dashboard() == {"status": "ok"}

    assert requested_urls == [
        "https://dashboard.example.test/api/topology",
        "https://dashboard.example.test/api/health",
    ]


@pytest.mark.unit
def test_drift_terminal_topic_is_declared_for_publishing() -> None:
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_demo_drift_detector/contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == "onex.evt.omnimarket.demo-drift-detected.v1"
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
