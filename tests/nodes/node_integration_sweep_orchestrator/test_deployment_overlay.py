# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sweep's hosts and endpoints come from the deployment overlay, never the package."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from omnimarket.models.node_overlay.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_integration_sweep_orchestrator.handlers.handler_integration_sweep_orchestrator import (
    HandlerIntegrationSweepOrchestrator,
    IntegrationSweepConfigurationError,
    load_deployment_overlay,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_integration_sweep_orchestrator_request import (
    ModelIntegrationSweepOrchestratorRequest,
)

NODE = "node_integration_sweep_orchestrator"
DEPLOYMENT_FIELDS = (
    "runtime_host",
    "runtime_repo_path",
    "stability_test_runtime_url",
    "container_health_host",
    "infra_runtime_host",
    "projection_api_url",
)
_PROBES = (
    "omnimarket.nodes.node_integration_sweep_orchestrator.handlers."
    "handler_integration_sweep_orchestrator"
)


def _request(
    tmp_path: Path, **overrides: Any
) -> ModelIntegrationSweepOrchestratorRequest:
    return ModelIntegrationSweepOrchestratorRequest(
        artifact_root=str(tmp_path / "cc"),
        artifact_date="2026-10-10",
        **overrides,
    )


def test_the_request_ships_no_deployment_fact() -> None:
    request = ModelIntegrationSweepOrchestratorRequest()
    assert {
        name: getattr(request, name) for name in DEPLOYMENT_FIELDS
    } == dict.fromkeys(DEPLOYMENT_FIELDS, "")


def test_overlay_fills_empty_fields_and_a_request_value_wins(
    tmp_path: Path, integration_sweep_deployment_overlay: Path
) -> None:
    resolved = HandlerIntegrationSweepOrchestrator._with_deployment(
        _request(tmp_path, container_health_host="request-host.example.invalid")
    )
    assert resolved.container_health_host == "request-host.example.invalid"
    assert resolved.runtime_host == "192.0.2.10"
    assert resolved.runtime_repo_path == "/srv/runtime/repo"
    assert resolved.stability_test_runtime_url == "http://192.0.2.10:18085"
    assert resolved.infra_runtime_host == "192.0.2.10"
    assert resolved.projection_api_url == "http://192.0.2.10:3002"


def test_dry_run_without_an_overlay_names_how_to_supply_one(tmp_path: Path) -> None:
    result = HandlerIntegrationSweepOrchestrator().handle(
        _request(tmp_path, dry_run=True)
    )
    assert result.status == "blocked"
    invalid = [s for s in result.surfaces if s["status"] == "invalid"]
    assert {s["surface"] for s in invalid} == {"RUNTIME_HEALTH", "CONTAINER_HEALTH"}
    assert all("ONEX_SKILL_OVERLAY_ROOTS" in s["reason"] for s in invalid)


def test_wet_run_without_an_overlay_probes_nothing_and_reports_not_configured(
    tmp_path: Path,
) -> None:
    with (
        patch(f"{_PROBES}.probe_runtime_health") as health,
        patch(f"{_PROBES}.probe_container_health") as containers,
        patch(
            f"{_PROBES}.probe_github_ci",
            return_value={"surface": "GITHUB_CI", "status": "pass", "details": {}},
        ),
    ):
        result = HandlerIntegrationSweepOrchestrator().handle(
            _request(tmp_path, kafka_topics=["t"], projection_topics=["p"])
        )
    health.assert_not_called()
    containers.assert_not_called()
    by_surface = {s["surface"]: s for s in result.surfaces}
    for surface in ("RUNTIME_HEALTH", "CONTAINER_HEALTH", "KAFKA", "PROJECTION"):
        assert by_surface[surface]["status"] == "error"
        assert "is not configured" in by_surface[surface]["details"]["error"]
    assert result.status == "blocked"


def test_runtime_sha_checks_without_a_host_refuse_before_writing_a_receipt(
    tmp_path: Path,
) -> None:
    contracts = tmp_path / "cc" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "OMN-1.yaml").write_text(
        yaml.safe_dump(
            {
                "ticket_id": "OMN-1",
                "dod_evidence": [
                    {
                        "id": "dod-sha",
                        "checks": [
                            {
                                "check_type": "runtime_sha_match",
                                "check_value": "abc1234def",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(IntegrationSweepConfigurationError, match="runtime_host"):
        HandlerIntegrationSweepOrchestrator().handle(
            _request(tmp_path, tickets=["OMN-1"], run_surface_probes=False)
        )
    assert not (tmp_path / "cc" / "drift").exists()


def test_overlay_with_an_unknown_key_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "overlays" / NODE / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({"runtime_hots": "x"}), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    with pytest.raises(NodeOverlayError, match="runtime_hots"):
        load_deployment_overlay()
