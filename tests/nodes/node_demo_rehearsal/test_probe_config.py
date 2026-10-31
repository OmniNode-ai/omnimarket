# SPDX-License-Identifier: MIT
"""Unit tests for HandlerDemoRehearsal dashboard probe configuration."""

from __future__ import annotations

import pytest

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_demo_probe import (
    ModelDemoProbeOverlay,
    ModelDemoProbePolicy,
)
from omnimarket.nodes.node_demo_rehearsal.handlers import (
    handler_demo_rehearsal as handler_module,
)

_PROBES = [
    ("_probe_topology", "/api/topology"),
    ("_probe_dashboard_api", "/api/health"),
]


@pytest.fixture
def recording_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[float], list[str]]:
    timeouts: list[float] = []
    urls: list[str] = []

    class Response:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {}

    class RecordingClient:
        def __init__(self, *, timeout: float) -> None:
            timeouts.append(timeout)

        async def __aenter__(self) -> RecordingClient:
            return self

        async def __aexit__(
            self, exc_type: object, exc: object, traceback: object
        ) -> None:
            pass

        async def get(self, url: str) -> Response:
            urls.append(url)
            return Response()

    monkeypatch.setattr(handler_module.httpx, "AsyncClient", RecordingClient)
    return timeouts, urls


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("probe_name", "path"), _PROBES)
async def test_injected_overlay_and_policy(
    monkeypatch: pytest.MonkeyPatch,
    recording_client: tuple[list[float], list[str]],
    probe_name: str,
    path: str,
) -> None:
    monkeypatch.setenv("DEMO_DASHBOARD_URL", "http://other.example.test:8080")
    overlay = ModelDemoProbeOverlay(dashboard_url="http://dashboard.example.test:8080")
    policy = ModelDemoProbePolicy(timeout_seconds=2.5)
    handler = handler_module.HandlerDemoRehearsal(overlay=overlay, policy=policy)

    assert await getattr(handler, probe_name)() == {}

    timeouts, urls = recording_client
    assert urls == [f"{overlay.dashboard_url}{path}"]
    assert timeouts == [policy.timeout_seconds]
    resolved_overlay, resolved_policy = handler._probe_config()
    assert resolved_overlay is overlay
    assert resolved_policy is policy


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("probe_name", "path"), _PROBES)
async def test_default_policy_from_contract_without_base_init(
    monkeypatch: pytest.MonkeyPatch,
    recording_client: tuple[list[float], list[str]],
    probe_name: str,
    path: str,
) -> None:
    class HandlerWithoutInit(handler_module.HandlerDemoRehearsal):
        def __init__(self) -> None:
            pass

    dashboard_url = "http://dashboard.example.test:8080"
    monkeypatch.setenv("DEMO_DASHBOARD_URL", dashboard_url)
    contract_policy = ModelDemoProbePolicy.model_validate(
        load_packaged_contract_section("node_demo_rehearsal", "probe_policy")
    )
    handler = HandlerWithoutInit()

    assert await getattr(handler, probe_name)() == {}

    overlay, policy = handler._probe_config()
    assert policy.timeout_seconds == contract_policy.timeout_seconds
    timeouts, urls = recording_client
    assert timeouts == [contract_policy.timeout_seconds]
    assert urls == [f"{dashboard_url}{path}"]
    monkeypatch.delenv("DEMO_DASHBOARD_URL")
    cached_overlay, cached_policy = handler._probe_config()
    assert cached_overlay is overlay
    assert cached_policy is policy


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("probe_name", "path"), _PROBES)
async def test_overlay_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    recording_client: tuple[list[float], list[str]],
    probe_name: str,
    path: str,
) -> None:
    dashboard_url = "http://dashboard.example.test:8080"
    monkeypatch.setenv("DEMO_DASHBOARD_URL", dashboard_url)
    handler = handler_module.HandlerDemoRehearsal(
        policy=ModelDemoProbePolicy(timeout_seconds=2.5)
    )

    assert await getattr(handler, probe_name)() == {}

    timeouts, urls = recording_client
    assert urls == [f"{dashboard_url}{path}"]
    assert timeouts == [2.5]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("probe_name", [probe_name for probe_name, _ in _PROBES])
async def test_missing_overlay_raises_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
    recording_client: tuple[list[float], list[str]],
    probe_name: str,
) -> None:
    monkeypatch.delenv("DEMO_DASHBOARD_URL", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    handler = handler_module.HandlerDemoRehearsal(
        policy=ModelDemoProbePolicy(timeout_seconds=2.5)
    )

    with pytest.raises(NodeOverlayError, match="DEMO_DASHBOARD_URL"):
        await getattr(handler, probe_name)()

    timeouts, urls = recording_client
    assert timeouts == []
    assert urls == []
