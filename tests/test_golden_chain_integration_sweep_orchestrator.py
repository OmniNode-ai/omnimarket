# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-9334 reason="test fixture — uses .201 lab endpoint as integration-sweep test input; not a runtime default or shipping connection string"
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
from omnibase_core.enums.ticket.enum_receipt_status import EnumReceiptStatus
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt
from omnibase_core.validation.runtime_sha_match import CHECK_TYPE_RUNTIME_SHA_MATCH

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


def test_integration_sweep_writes_runtime_sha_receipt_for_stale_runtime(
    tmp_path: Path,
) -> None:
    merge_sha = "abc123def456"  # pragma: allowlist secret
    stale_sha = "deadbeef0000"  # pragma: allowlist secret
    _write_runtime_sha_contract(tmp_path / "contracts", "OMN-9334", merge_sha)

    result = HandlerIntegrationSweepOrchestrator(
        runtime_sha_handler=_StubRuntimeShaHandler(
            ticket_id="OMN-9334",
            evidence_item_id="dod-runtime-sha",
            merge_sha=merge_sha,
            deployed_sha=stale_sha,
        )
    ).handle(
        ModelIntegrationSweepOrchestratorRequest(
            scope="explicit",
            tickets=["OMN-9334"],
            artifact_root=str(tmp_path),
            artifact_date="2026-04-30",
        )
    )

    receipt_path = (
        tmp_path
        / "drift"
        / "dod_receipts"
        / "OMN-9334"
        / "dod-runtime-sha"
        / "runtime_sha_match.yaml"
    )
    artifact = yaml.safe_load(Path(result.artifact_path).read_text(encoding="utf-8"))
    receipt = yaml.safe_load(receipt_path.read_text(encoding="utf-8"))

    assert result.status == "blocked"
    assert result.details["runtime_sha_stale"] == "1"
    assert artifact["status"] == "blocked"
    assert artifact["runtime_sha_match"][0]["status"] == "FAIL"
    assert receipt["check_type"] == CHECK_TYPE_RUNTIME_SHA_MATCH
    assert receipt["check_value"] == merge_sha
    assert receipt["status"] == "FAIL"


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


class _StubRuntimeShaHandler:
    def __init__(
        self,
        *,
        ticket_id: str,
        evidence_item_id: str,
        merge_sha: str,
        deployed_sha: str,
    ) -> None:
        self._ticket_id = ticket_id
        self._evidence_item_id = evidence_item_id
        self._merge_sha = merge_sha
        self._deployed_sha = deployed_sha

    def handle(self, request: object) -> ModelDodReceipt:
        match = self._deployed_sha == self._merge_sha
        return ModelDodReceipt(
            schema_version="1.0.0",
            ticket_id=self._ticket_id,
            evidence_item_id=self._evidence_item_id,
            check_type=CHECK_TYPE_RUNTIME_SHA_MATCH,
            check_value=self._merge_sha,
            status=EnumReceiptStatus.PASS if match else EnumReceiptStatus.FAIL,
            run_timestamp=datetime.now(tz=UTC),
            commit_sha=self._deployed_sha,
            runner="integration-sweep-verifier",
            verifier="integration-sweep-test-verifier",
            probe_command="ssh 192.168.86.201 git -C /data/omninode/omni_home/omnimarket rev-parse HEAD",  # onex-allow-internal-ip: test fixture
            probe_stdout=f"{self._deployed_sha}\n",
            actual_output=json.dumps(
                {
                    "runtime_host": "192.168.86.201",  # onex-allow-internal-ip: test fixture
                    "deployed_sha": self._deployed_sha,
                    "merge_sha": self._merge_sha,
                    "match": match,
                }
            ),
        )


def _write_runtime_sha_contract(
    contracts_dir: Path, ticket_id: str, merge_sha: str
) -> None:
    contracts_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticket_id": ticket_id,
        "title": "Runtime SHA gate",
        "dod_evidence": [
            {
                "id": "dod-runtime-sha",
                "description": "Runtime SHA matches merge SHA",
                "checks": [
                    {
                        "check_type": CHECK_TYPE_RUNTIME_SHA_MATCH,
                        "check_value": merge_sha,
                    }
                ],
            }
        ],
    }
    (contracts_dir / f"{ticket_id}.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=True),
        encoding="utf-8",
    )
