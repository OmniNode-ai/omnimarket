from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_integration_sweep_orchestrator.handlers.handler_integration_sweep_orchestrator import (
    HandlerIntegrationSweepOrchestrator,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_integration_sweep_orchestrator_request import (
    ModelIntegrationSweepOrchestratorRequest,
)


def test_integration_sweep_writes_drift_artifact(tmp_path: Path) -> None:
    result = HandlerIntegrationSweepOrchestrator().handle(
        ModelIntegrationSweepOrchestratorRequest(
            scope="explicit",
            tickets=["OMN-10409"],
            artifact_root=str(tmp_path),
            artifact_date="2026-04-30",
        )
    )

    artifact_path = Path(result.artifact_path)
    assert result.status == "recorded"
    assert result.artifact_written is True
    assert result.ticket_count == 1
    assert artifact_path == tmp_path / "drift" / "integration" / "2026-04-30.yaml"
    assert artifact_path.is_file()

    artifact = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
    assert artifact["artifact_type"] == "ModelIntegrationRecord"
    assert artifact["tickets"] == ["OMN-10409"]
    assert artifact["status"] == "recorded"


def test_integration_sweep_dry_run_does_not_write(tmp_path: Path) -> None:
    result = HandlerIntegrationSweepOrchestrator().handle(
        ModelIntegrationSweepOrchestratorRequest(
            tickets=["OMN-10409"],
            artifact_root=str(tmp_path),
            artifact_date="2026-04-30",
            dry_run=True,
        )
    )

    assert result.artifact_written is False
    assert not Path(result.artifact_path).exists()


def test_contract_declares_node_as_implemented() -> None:
    contract_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_integration_sweep_orchestrator"
        / "contract.yaml"
    )
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))

    assert raw.get("node_not_implemented") is not True
    assert raw["terminal_event"] == "onex.evt.omnimarket.integration-sweep-completed.v1"
