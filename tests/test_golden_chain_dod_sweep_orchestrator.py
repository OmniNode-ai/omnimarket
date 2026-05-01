from __future__ import annotations

import json
from pathlib import Path

import yaml

from omnimarket.nodes.node_dod_sweep_orchestrator.handlers.handler_dod_sweep_orchestrator import (
    HandlerDodSweepOrchestrator,
)
from omnimarket.nodes.node_dod_sweep_orchestrator.models.model_dod_sweep_orchestrator_request import (
    ModelDodSweepOrchestratorRequest,
)


def test_targeted_sweep_writes_ticket_receipt(tmp_path: Path) -> None:
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    (contracts_dir / "OMN-10408.yaml").write_text(
        "ticket_id: OMN-10408\ndod_evidence: []\n",
        encoding="utf-8",
    )

    result = HandlerDodSweepOrchestrator().handle(
        ModelDodSweepOrchestratorRequest(
            scope="OMN-10408",
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
        )
    )

    receipt_path = Path(result.receipt_path)
    assert result.status == "verified"
    assert result.receipt_written is True
    assert result.contract_exists is True
    assert receipt_path == tmp_path / ".evidence" / "OMN-10408" / "dod_report.json"
    assert receipt_path.is_file()

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["ticket_id"] == "OMN-10408"
    assert receipt["result"]["failed"] == 0
    assert receipt["checks"][0]["status"] == "pass"


def test_targeted_sweep_records_missing_contract_receipt(tmp_path: Path) -> None:
    result = HandlerDodSweepOrchestrator().handle(
        ModelDodSweepOrchestratorRequest(
            scope="OMN-10408",
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
        )
    )

    receipt = json.loads(Path(result.receipt_path).read_text(encoding="utf-8"))
    assert result.status == "missing_contract"
    assert result.failed == 1
    assert receipt["result"]["failed"] == 1
    assert receipt["checks"][0]["status"] == "fail"


def test_dry_run_does_not_write_receipt(tmp_path: Path) -> None:
    result = HandlerDodSweepOrchestrator().handle(
        ModelDodSweepOrchestratorRequest(
            scope="OMN-10408",
            evidence_root=str(tmp_path),
            dry_run=True,
        )
    )

    assert result.receipt_written is False
    assert not Path(result.receipt_path).exists()


def test_contract_declares_node_as_implemented() -> None:
    contract_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_dod_sweep_orchestrator"
        / "contract.yaml"
    )
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))

    assert raw.get("node_not_implemented") is not True
    assert raw["terminal_event"] == "onex.evt.omnimarket.dod-sweep-completed.v1"
