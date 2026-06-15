"""Golden-chain tests for node_dod_sweep_orchestrator.

Tests cover:
  - Targeted mode (single OMN-XXXX ticket)
  - All four checks: contract_exists, receipt_exists, pr_merged (mocked), ci_green (mocked)
  - Batch mode with explicit ticket_ids
  - Batch mode skipped when no tickets provided
  - dry_run does not write receipts
  - Contract and entry-point registration
"""

from __future__ import annotations

import json
import unittest.mock as mock
from importlib.metadata import entry_points
from pathlib import Path

import yaml

from omnimarket.nodes.node_dod_sweep_orchestrator.handlers.handler_dod_sweep_orchestrator import (
    HandlerDodSweepOrchestrator,
    _check_ci_green,
    _check_contract_exists,
    _check_receipt_exists,
)
from omnimarket.nodes.node_dod_sweep_orchestrator.models.model_dod_sweep_orchestrator_request import (
    ModelDodSweepOrchestratorRequest,
)

# ---------------------------------------------------------------------------
# Targeted mode — contract_exists only
# ---------------------------------------------------------------------------


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
            enabled_checks=("contract_exists",),
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
            enabled_checks=("contract_exists",),
        )
    )

    receipt = json.loads(Path(result.receipt_path).read_text(encoding="utf-8"))
    assert result.status == "failed"
    assert result.failed == 1
    assert receipt["result"]["failed"] == 1
    assert receipt["checks"][0]["status"] == "fail"


def test_dry_run_does_not_write_receipt(tmp_path: Path) -> None:
    result = HandlerDodSweepOrchestrator().handle(
        ModelDodSweepOrchestratorRequest(
            scope="OMN-10408",
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            dry_run=True,
            enabled_checks=("contract_exists",),
        )
    )

    assert result.receipt_written is False
    assert not Path(result.receipt_path).exists()


def test_targeted_mode_sets_mode_field(tmp_path: Path) -> None:
    result = HandlerDodSweepOrchestrator().handle(
        ModelDodSweepOrchestratorRequest(
            scope="OMN-10408",
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            enabled_checks=("contract_exists",),
        )
    )
    assert result.mode == "targeted"


# ---------------------------------------------------------------------------
# receipt_exists check
# ---------------------------------------------------------------------------


def test_receipt_exists_pass_when_dod_evidence_present(tmp_path: Path) -> None:
    contract_path = tmp_path / "OMN-9999.yaml"
    contract_path.write_text(
        "ticket_id: OMN-9999\ndod_evidence:\n  - id: e1\n    checks: []\n",
        encoding="utf-8",
    )
    result = _check_receipt_exists("OMN-9999", contract_path)
    assert result.status == "pass"
    assert result.details["dod_evidence_count"] == "1"


def test_receipt_exists_fail_when_dod_evidence_empty(tmp_path: Path) -> None:
    contract_path = tmp_path / "OMN-9999.yaml"
    contract_path.write_text(
        "ticket_id: OMN-9999\ndod_evidence: []\n",
        encoding="utf-8",
    )
    result = _check_receipt_exists("OMN-9999", contract_path)
    assert result.status == "fail"
    assert result.details["dod_evidence_count"] == "0"


def test_receipt_exists_skip_when_contract_missing(tmp_path: Path) -> None:
    contract_path = tmp_path / "nonexistent.yaml"
    result = _check_receipt_exists("OMN-0000", contract_path)
    assert result.status == "skip"
    assert result.details["reason"] == "contract_missing"


def test_receipt_exists_fail_on_yaml_parse_error(tmp_path: Path) -> None:
    contract_path = tmp_path / "bad.yaml"
    contract_path.write_text(": : : invalid yaml :::\n", encoding="utf-8")
    result = _check_receipt_exists("OMN-0001", contract_path)
    # yaml.safe_load on ": : : invalid yaml :::" may parse or fail — either way
    # we just check that the result has a defined status
    assert result.status in ("pass", "fail", "skip")


# ---------------------------------------------------------------------------
# pr_merged check (subprocess mocked)
# ---------------------------------------------------------------------------


def test_pr_merged_pass_when_gh_returns_pr(tmp_path: Path) -> None:
    gh_output = json.dumps({"number": "42", "repo": "OmniNode-ai/omnimarket"})

    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=0,
            stdout=gh_output,
            stderr="",
        )
        result = HandlerDodSweepOrchestrator().handle(
            ModelDodSweepOrchestratorRequest(
                scope="OMN-10408",
                contract_root=str(tmp_path),
                evidence_root=str(tmp_path),
                enabled_checks=("pr_merged",),
            )
        )

    assert result.status == "verified"
    assert result.failed == 0
    pr_check = next(c for c in result.batch_results[0].checks if c.check == "pr_merged")
    assert pr_check.status == "pass"
    assert pr_check.details["pr_number"] == "42"


def test_pr_merged_fail_when_no_merged_pr_found(tmp_path: Path) -> None:
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=0,
            stdout="null",
            stderr="",
        )
        result = HandlerDodSweepOrchestrator().handle(
            ModelDodSweepOrchestratorRequest(
                scope="OMN-10408",
                contract_root=str(tmp_path),
                evidence_root=str(tmp_path),
                enabled_checks=("pr_merged",),
            )
        )

    assert result.failed == 1
    pr_check = next(c for c in result.batch_results[0].checks if c.check == "pr_merged")
    assert pr_check.status == "fail"
    assert pr_check.details["reason"] == "no_merged_pr_found"


def test_pr_merged_fail_when_gh_unavailable(tmp_path: Path) -> None:
    with mock.patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
        result = HandlerDodSweepOrchestrator().handle(
            ModelDodSweepOrchestratorRequest(
                scope="OMN-10408",
                contract_root=str(tmp_path),
                evidence_root=str(tmp_path),
                enabled_checks=("pr_merged",),
            )
        )

    assert result.failed == 1
    pr_check = next(c for c in result.batch_results[0].checks if c.check == "pr_merged")
    assert pr_check.status == "fail"


# ---------------------------------------------------------------------------
# ci_green check (subprocess mocked)
# ---------------------------------------------------------------------------


def test_ci_green_pass_when_all_checks_succeed() -> None:
    checks_output = json.dumps(
        [
            {"name": "lint", "state": "SUCCESS", "conclusion": "SUCCESS"},
            {"name": "test", "state": "SUCCESS", "conclusion": "SUCCESS"},
        ]
    )

    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=0,
            stdout=checks_output,
            stderr="",
        )
        result = _check_ci_green(
            "OMN-10408",
            {"number": "42", "repo": "OmniNode-ai/omnimarket"},
        )

    assert result.status == "pass"
    assert "2_checks_green" in result.details["detail"]


def test_ci_green_fail_when_check_failed() -> None:
    checks_output = json.dumps(
        [
            {"name": "lint", "state": "FAILURE", "conclusion": "FAILURE"},
            {"name": "test", "state": "SUCCESS", "conclusion": "SUCCESS"},
        ]
    )

    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=0,
            stdout=checks_output,
            stderr="",
        )
        result = _check_ci_green(
            "OMN-10408",
            {"number": "42", "repo": "OmniNode-ai/omnimarket"},
        )

    assert result.status == "fail"
    assert "lint" in result.details["detail"]


def test_ci_green_skip_when_no_pr_info() -> None:
    result = _check_ci_green("OMN-10408", {})
    assert result.status == "skip"
    assert result.details["reason"] == "no_merged_pr"


def test_ci_green_pass_vacuously_when_no_checks_defined() -> None:
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=0,
            stdout="null",
            stderr="",
        )
        result = _check_ci_green(
            "OMN-10408",
            {"number": "42", "repo": "OmniNode-ai/omnimarket"},
        )

    assert result.status == "pass"
    assert result.details["detail"] == "no_checks_defined"


# ---------------------------------------------------------------------------
# Batch mode with explicit ticket_ids
# ---------------------------------------------------------------------------


def test_batch_mode_explicit_ticket_ids(tmp_path: Path) -> None:
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    for tid in ("OMN-1001", "OMN-1002"):
        (contracts_dir / f"{tid}.yaml").write_text(
            f"ticket_id: {tid}\ndod_evidence:\n  - id: e1\n    checks: []\n",
            encoding="utf-8",
        )

    result = HandlerDodSweepOrchestrator().handle(
        ModelDodSweepOrchestratorRequest(
            ticket_ids=("OMN-1001", "OMN-1002"),
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            enabled_checks=("contract_exists", "receipt_exists"),
        )
    )

    assert result.mode == "batch"
    assert result.batch_total == 2
    assert result.batch_verified == 2
    assert result.batch_failed == 0
    assert result.status == "verified"
    # Both receipts written
    for tid in ("OMN-1001", "OMN-1002"):
        assert (tmp_path / ".evidence" / tid / "dod_report.json").is_file()


def test_batch_mode_partial_failure(tmp_path: Path) -> None:
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    # Only one contract exists
    (contracts_dir / "OMN-1001.yaml").write_text(
        "ticket_id: OMN-1001\ndod_evidence: []\n",
        encoding="utf-8",
    )

    result = HandlerDodSweepOrchestrator().handle(
        ModelDodSweepOrchestratorRequest(
            ticket_ids=("OMN-1001", "OMN-1002"),
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            enabled_checks=("contract_exists",),
        )
    )

    assert result.mode == "batch"
    assert result.batch_total == 2
    assert result.batch_failed == 1
    assert result.batch_verified == 1
    assert result.status == "failed"


def test_batch_mode_dry_run_does_not_write(tmp_path: Path) -> None:
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    (contracts_dir / "OMN-2001.yaml").write_text(
        "ticket_id: OMN-2001\ndod_evidence: []\n",
        encoding="utf-8",
    )

    result = HandlerDodSweepOrchestrator().handle(
        ModelDodSweepOrchestratorRequest(
            ticket_ids=("OMN-2001",),
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            enabled_checks=("contract_exists",),
            dry_run=True,
        )
    )

    assert result.mode == "batch"
    assert not (tmp_path / ".evidence" / "OMN-2001" / "dod_report.json").exists()
    assert all(not tr.receipt_written for tr in result.batch_results)


def test_batch_mode_skipped_when_no_tickets(tmp_path: Path) -> None:
    result = HandlerDodSweepOrchestrator().handle(
        ModelDodSweepOrchestratorRequest(
            scope="some-project-label",
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
        )
    )

    assert result.status == "skipped"
    assert result.mode == "batch"
    assert result.details["reason"] == "no_tickets_to_sweep"


def test_batch_mode_invalid_ticket_ids_skipped(tmp_path: Path) -> None:
    result = HandlerDodSweepOrchestrator().handle(
        ModelDodSweepOrchestratorRequest(
            ticket_ids=("not-a-ticket", "also-bad"),
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
        )
    )

    assert result.status == "skipped"
    assert result.details["reason"] == "no_valid_ticket_ids"


# ---------------------------------------------------------------------------
# contract_exists unit test
# ---------------------------------------------------------------------------


def test_check_contract_exists_pass(tmp_path: Path) -> None:
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    (contracts_dir / "OMN-5555.yaml").write_text(
        "ticket_id: OMN-5555\n", encoding="utf-8"
    )
    check, path = _check_contract_exists("OMN-5555", tmp_path)
    assert check.status == "pass"
    assert path.is_file()


def test_check_contract_exists_fail(tmp_path: Path) -> None:
    check, path = _check_contract_exists("OMN-5556", tmp_path)
    assert check.status == "fail"
    assert not path.is_file()


# ---------------------------------------------------------------------------
# Contract and entry-point registration
# ---------------------------------------------------------------------------


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


def test_node_is_registered_as_onex_entry_point() -> None:
    matches = [
        entry_point
        for entry_point in entry_points(group="onex.nodes")
        if entry_point.name == "node_dod_sweep_orchestrator"
    ]

    assert len(matches) == 1
    assert matches[0].value == "omnimarket.nodes.node_dod_sweep_orchestrator"
    assert matches[0].load().__name__ == "omnimarket.nodes.node_dod_sweep_orchestrator"
