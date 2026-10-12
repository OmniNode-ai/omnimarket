# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The integration sweep's hosts and endpoints are deployment facts (OMN-20935).

A request value wins, a blank one reads the node overlay, and with neither the probe that
needs the fact reports ``error`` naming how to supply it. No host or endpoint is a default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_integration_sweep_orchestrator.handlers.handler_integration_sweep_orchestrator import (
    HandlerIntegrationSweepOrchestrator,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_integration_sweep_orchestrator_request import (
    ModelIntegrationSweepOrchestratorRequest,
)
from tests.node_overlay_support import INTEGRATION_SWEEP_OVERLAY, install_node_overlay

NODE = "node_integration_sweep_orchestrator"
FIELDS = tuple(INTEGRATION_SWEEP_OVERLAY)


@pytest.fixture(autouse=True)
def _no_ambient_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)


@pytest.mark.unit
def test_the_request_model_carries_no_deployment_value() -> None:
    request = ModelIntegrationSweepOrchestratorRequest()
    assert {name: getattr(request, name) for name in FIELDS} == dict.fromkeys(
        FIELDS, ""
    )


@pytest.mark.unit
def test_blank_fields_read_the_node_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(monkeypatch, tmp_path, NODE, INTEGRATION_SWEEP_OVERLAY)
    resolved = HandlerIntegrationSweepOrchestrator._with_deployment(
        ModelIntegrationSweepOrchestratorRequest()
    )
    assert {
        name: getattr(resolved, name) for name in FIELDS
    } == INTEGRATION_SWEEP_OVERLAY


@pytest.mark.unit
def test_a_request_value_wins_over_the_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(monkeypatch, tmp_path, NODE, INTEGRATION_SWEEP_OVERLAY)
    resolved = HandlerIntegrationSweepOrchestrator._with_deployment(
        ModelIntegrationSweepOrchestratorRequest(runtime_host="198.51.100.9")
    )
    assert resolved.runtime_host == "198.51.100.9"
    assert (
        resolved.infra_runtime_host == INTEGRATION_SWEEP_OVERLAY["infra_runtime_host"]
    )


@pytest.mark.unit
def test_without_an_overlay_the_baseline_probes_report_not_configured(
    tmp_path: Path,
) -> None:
    """Positive control for the zero: nothing is probed and nothing is green."""
    result = HandlerIntegrationSweepOrchestrator().handle(
        ModelIntegrationSweepOrchestratorRequest(
            scope="explicit",
            tickets=["OMN-10409"],
            artifact_root=str(tmp_path),
            artifact_date="2026-04-30",
            kafka_topics=["example.topic.v1"],
            db_tables=["example_table"],
            projection_topics=["example"],
        )
    )
    by_surface: dict[str, Any] = {entry["surface"]: entry for entry in result.surfaces}
    unconfigured = {
        "RUNTIME_HEALTH": "stability_test_runtime_url",
        "CONTAINER_HEALTH": "container_health_host",
        "KAFKA": "infra_runtime_host",
        "DB": "infra_runtime_host",
        "PROJECTION": "projection_api_url",
    }
    for surface, field in unconfigured.items():
        assert by_surface[surface]["status"] == "error", surface
        message = by_surface[surface]["details"]["error"]
        assert f"{field} is not configured" in message
        assert "ONEX_SKILL_OVERLAY_ROOTS" in message
    assert result.status == "blocked"


@pytest.mark.unit
def test_dry_run_marks_an_unconfigured_target_invalid(tmp_path: Path) -> None:
    result = HandlerIntegrationSweepOrchestrator().handle(
        ModelIntegrationSweepOrchestratorRequest(
            scope="explicit",
            tickets=["OMN-10409"],
            artifact_root=str(tmp_path),
            dry_run=True,
        )
    )
    invalid = [e for e in result.surfaces if e["status"] == "invalid"]
    assert {e["surface"] for e in invalid} == {"RUNTIME_HEALTH", "CONTAINER_HEALTH"}
    assert all("empty probe target" in e["reason"] for e in invalid)


@pytest.mark.unit
def test_runtime_sha_check_without_a_runtime_host_refuses(tmp_path: Path) -> None:
    """A wet run that must SSH to verify a runtime SHA refuses without a host."""
    import yaml
    from omnibase_core.validation.runtime_sha_match import CHECK_TYPE_RUNTIME_SHA_MATCH

    from omnimarket.handlers.node_overlay_reader import NodeOverlayError

    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "OMN-10409.yaml").write_text(
        yaml.safe_dump(
            {
                "ticket_id": "OMN-10409",
                "title": "Runtime SHA gate",
                "dod_evidence": [
                    {
                        "id": "dod-runtime-sha",
                        "description": "Runtime SHA matches merge SHA",
                        "checks": [
                            {
                                "check_type": CHECK_TYPE_RUNTIME_SHA_MATCH,
                                "check_value": "a1" * 6,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(NodeOverlayError, match="runtime_host is not configured"):
        HandlerIntegrationSweepOrchestrator().handle(
            ModelIntegrationSweepOrchestratorRequest(
                scope="explicit",
                tickets=["OMN-10409"],
                artifact_root=str(tmp_path),
                artifact_date="2026-04-30",
                run_surface_probes=False,
            )
        )
