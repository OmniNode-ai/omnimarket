# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration proof for node_dod_sweep_orchestrator (OMN-13679).

WS-5. ``HandlerDodSweepOrchestrator.handle`` runs four DoD checks per ticket:
``contract_exists`` / ``receipt_exists`` (pure filesystem) and ``pr_merged`` /
``ci_green`` (live ``gh`` subprocess). The integration test restricts
``enabled_checks`` to the two filesystem checks and points ``contract_root`` /
``evidence_root`` at a synthetic ``contracts/<TICKET>.yaml`` tree under
``tmp_path``. This exercises the real targeted/batch routing, per-check status
roll-up, and receipt-write logic deterministically with NO subprocess and no
monkeypatching of the ``gh``/subprocess boundary.

Param axes: targeted verified, targeted missing-contract (NEGATIVE CONTROL),
targeted empty-dod_evidence (NEGATIVE CONTROL), dry-run receipt suppression,
batch mixed pass/fail, and the no-tickets skip path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_dod_sweep_orchestrator.handlers.handler_dod_sweep_orchestrator import (
    HandlerDodSweepOrchestrator,
)
from omnimarket.nodes.node_dod_sweep_orchestrator.models.model_dod_sweep_orchestrator_request import (
    ModelDodSweepOrchestratorRequest,
)

_FS_CHECKS = ("contract_exists", "receipt_exists")


def _write_contract(root: Path, ticket_id: str, *, with_evidence: bool) -> None:
    """Write a contracts/<TICKET>.yaml under ``root`` (the OCC-style layout)."""
    contracts_dir = root / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    if with_evidence:
        body = (
            "schema_version: 1.0.0\n"
            f"ticket_id: {ticket_id}\n"
            "dod_evidence:\n"
            "  - id: dod-001\n"
            "    description: Code change shipped\n"
            "    checks:\n"
            "      - check_type: command\n"
            "        check_value: 'true'\n"
        )
    else:
        body = f"schema_version: 1.0.0\nticket_id: {ticket_id}\ndod_evidence: []\n"
    (contracts_dir / f"{ticket_id}.yaml").write_text(body, encoding="utf-8")


@pytest.mark.integration
def test_targeted_verified_writes_receipt(tmp_path: Path) -> None:
    _write_contract(tmp_path, "OMN-13679", with_evidence=True)
    result = HandlerDodSweepOrchestrator().handle(
        ModelDodSweepOrchestratorRequest(
            scope="OMN-13679",
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            enabled_checks=_FS_CHECKS,
        )
    )

    assert result.mode == "targeted"
    assert result.status == "verified"
    assert result.contract_exists is True
    assert result.failed == 0
    assert result.receipt_written is True
    # The receipt file must actually land on disk with the verified status.
    receipt = Path(result.receipt_path)
    assert receipt.is_file()
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["result"]["status"] == "verified"


@pytest.mark.integration
def test_targeted_missing_contract_fails(tmp_path: Path) -> None:
    # NEGATIVE CONTROL: no contract file → contract_exists fails the sweep.
    result = HandlerDodSweepOrchestrator().handle(
        ModelDodSweepOrchestratorRequest(
            scope="OMN-99999",
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            enabled_checks=_FS_CHECKS,
            dry_run=True,
        )
    )

    assert result.mode == "targeted"
    assert result.status == "failed"
    assert result.contract_exists is False
    assert result.failed >= 1


@pytest.mark.integration
def test_targeted_empty_evidence_fails(tmp_path: Path) -> None:
    # NEGATIVE CONTROL: contract present but dod_evidence is empty → fail.
    _write_contract(tmp_path, "OMN-555", with_evidence=False)
    result = HandlerDodSweepOrchestrator().handle(
        ModelDodSweepOrchestratorRequest(
            scope="OMN-555",
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            enabled_checks=_FS_CHECKS,
            dry_run=True,
        )
    )

    assert result.status == "failed"
    assert result.contract_exists is True
    receipt_check = next(
        c
        for tr in result.batch_results
        for c in tr.checks
        if c.check == "receipt_exists"
    )
    assert receipt_check.status == "fail"


@pytest.mark.integration
def test_targeted_dry_run_suppresses_receipt_write(tmp_path: Path) -> None:
    _write_contract(tmp_path, "OMN-13679", with_evidence=True)
    result = HandlerDodSweepOrchestrator().handle(
        ModelDodSweepOrchestratorRequest(
            scope="OMN-13679",
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            enabled_checks=_FS_CHECKS,
            dry_run=True,
        )
    )

    assert result.status == "verified"
    assert result.receipt_written is False
    assert not Path(result.receipt_path).exists()


@pytest.mark.integration
def test_batch_mixed_pass_fail(tmp_path: Path) -> None:
    _write_contract(tmp_path, "OMN-100", with_evidence=True)
    # OMN-200 contract intentionally absent → that ticket fails.
    result = HandlerDodSweepOrchestrator().handle(
        ModelDodSweepOrchestratorRequest(
            scope="batch-label",
            ticket_ids=("OMN-100", "OMN-200"),
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            enabled_checks=_FS_CHECKS,
            dry_run=True,
        )
    )

    assert result.mode == "batch"
    assert result.batch_total == 2
    assert result.batch_verified == 1
    assert result.batch_failed == 1
    assert result.status == "failed"
    statuses = {tr.ticket_id: tr.status for tr in result.batch_results}
    assert statuses["OMN-100"] == "verified"
    assert statuses["OMN-200"] == "failed"


@pytest.mark.integration
def test_batch_no_tickets_skipped(tmp_path: Path) -> None:
    result = HandlerDodSweepOrchestrator().handle(
        ModelDodSweepOrchestratorRequest(
            scope="some-project-label",
            contract_root=str(tmp_path),
            evidence_root=str(tmp_path),
            enabled_checks=_FS_CHECKS,
        )
    )

    assert result.mode == "batch"
    assert result.status == "skipped"
    assert result.skipped == 1
    assert result.details.get("reason") == "no_tickets_to_sweep"
