# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15380 — dod_verify must fail CLOSED when it verifies zero checks.

Prior behaviour (OMN-15172, comment 217f33bc, run 66d7a41a-7817-49ae-8ab9-
cd72b4cd378b): a ticket with no OCC contract produced an inner state of
``status=skipped, total_checks=1, verified_count=0`` but the CLI exited 0 and
the RuntimeLocal-mediated ``onex skill dod_verify`` envelope reported
``exit_code=0 / status=success``. Absence of a contract — the highest-risk
input (nothing was checked at all) — was reported as green.

This module proves, at three layers, that a missing/unresolvable contract
(and, more generally, a verified_count == 0 outcome) is now fail-closed:

1. ``HandlerDodVerify`` — inner state carries a distinct machine-checkable
   reason (``CONTRACT_MISSING`` / ``NO_CHECKS_VERIFIED``) in ``error_message``.
2. ``RuntimeLocal._classify_result`` — the SAME generic classifier the
   ``onex skill dod_verify`` / RuntimeLocal-mediated envelope actually calls
   (not a reimplementation) maps the handler's dict result to FAILED, so the
   envelope-level exit_code/status defect is closed without touching
   omnibase_core.
3. ``python -m omnimarket.nodes.node_dod_verify`` CLI subprocess — the direct
   invocation exits non-zero and the persisted receipt (when
   ONEX_EVIDENCE_ROOT is set) is FAIL, not PASS.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)
from tests.runtime_local_compat import RuntimeLocal


def _no_contract_payload(ticket_id: str = "OMN-NOEXIST-15380") -> dict[str, object]:
    return {
        "correlation_id": str(uuid4()),
        "ticket_id": ticket_id,
        "dry_run": False,
        "requested_at": datetime.now(tz=UTC).isoformat(),
    }


@pytest.mark.unit
class TestHandlerReportsDistinctReason:
    """Layer 1: the inner state carries a distinct, non-vacuous reason."""

    def test_missing_contract_sets_contract_missing_reason(self) -> None:
        handler = HandlerDodVerify()
        result = handler.handle(_no_contract_payload("OMN-NOEXIST-15380"))

        assert isinstance(result, dict)
        assert result["status"] == "skipped"
        assert result["verified_count"] == 0
        error_message = result.get("error_message")
        assert error_message is not None, (
            "missing contract must populate error_message — a bare 'skipped' "
            "status with no error_message is indistinguishable from a benign "
            "skip at every downstream consumer that only reads error_message"
        )
        assert error_message.startswith("CONTRACT_MISSING"), error_message
        assert "OMN-NOEXIST-15380" in error_message

    def test_all_checks_skipped_with_contract_present_sets_generic_reason(
        self,
    ) -> None:
        """A contract that DOES exist but whose checks were all skipped for
        other reasons gets the general NO_CHECKS_VERIFIED reason, distinct
        from CONTRACT_MISSING — this is the OMN-14552-class general rule
        (verified_count == 0 is never a pass), not just the missing-contract
        special case.
        """
        handler = HandlerDodVerify()
        checks = [
            ModelEvidenceCheckResult(
                evidence_id="dod-001",
                description="API health",
                status=EnumEvidenceCheckStatus.SKIPPED,
            ),
            ModelEvidenceCheckResult(
                evidence_id="dod-002",
                description="Endpoint check",
                status=EnumEvidenceCheckStatus.SKIPPED,
            ),
        ]
        from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
            ModelDodVerifyStartCommand,
        )

        command = ModelDodVerifyStartCommand(
            correlation_id=uuid4(),
            ticket_id="OMN-ALLSKIP-15380",
            requested_at=datetime.now(tz=UTC),
        )
        state = handler._handle_typed(command, checks)

        assert state.status == EnumDodVerifyStatus.SKIPPED
        assert state.error_message is not None
        assert state.error_message.startswith("NO_CHECKS_VERIFIED")
        assert not state.error_message.startswith("CONTRACT_MISSING")

    def test_verified_run_has_no_error_message(self) -> None:
        """Non-regression: a genuine VERIFIED run stays clean (no reason)."""
        handler = HandlerDodVerify()
        from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
            ModelDodVerifyStartCommand,
        )

        command = ModelDodVerifyStartCommand(
            correlation_id=uuid4(),
            ticket_id="OMN-VERIFIED-15380",
            requested_at=datetime.now(tz=UTC),
        )
        checks = [
            ModelEvidenceCheckResult(
                evidence_id="dod-001",
                description="Tests pass",
                status=EnumEvidenceCheckStatus.VERIFIED,
            )
        ]
        state = handler._handle_typed(command, checks)

        assert state.status == EnumDodVerifyStatus.VERIFIED
        assert state.error_message is None


@pytest.mark.unit
class TestRuntimeLocalClassifiesMissingContractAsFailed:
    """Layer 2: the ACTUAL generic classifier the RuntimeLocal-mediated
    ``onex skill dod_verify`` envelope calls (``RuntimeLocal._classify_result``)
    maps the missing-contract dict to FAILED — proving the envelope-level
    exit_code/status defect from OMN-15172 is closed, using the real
    production function rather than a reimplementation of its logic.
    """

    def test_missing_contract_result_classifies_failed(self) -> None:
        from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

        handler = HandlerDodVerify()
        result_dict = handler.handle(_no_contract_payload("OMN-NOEXIST-15380"))

        classified = RuntimeLocal._classify_result(result_dict)

        assert classified == EnumWorkflowResult.FAILED, (
            f"RuntimeLocal classified a missing-contract (0 verified) result as "
            f"{classified!r} — the onex skill dod_verify envelope would report "
            "exit_code 0 / status success for a ticket with NO contract"
        )

    def test_verified_result_still_classifies_completed(self) -> None:
        """Non-regression: a genuine VERIFIED run still classifies COMPLETED."""
        from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

        handler = HandlerDodVerify()
        payload = _no_contract_payload("OMN-VERIFIED-CLASSIFY-15380")
        result_dict = handler.handle(
            payload,
        )
        # Force a VERIFIED-shaped dict directly (dict path has no injectable
        # evidence_results) — construct the state the handler would produce
        # for an all-VERIFIED run and confirm the classifier still passes it.
        result_dict = dict(result_dict)
        result_dict["status"] = "verified"
        result_dict["verified_count"] = 1
        result_dict["error_message"] = None

        classified = RuntimeLocal._classify_result(result_dict)

        assert classified == EnumWorkflowResult.COMPLETED


@pytest.mark.unit
class TestCliExitsNonZeroOnMissingContract:
    """Layer 3: the direct CLI invocation fails closed too."""

    def _run_main(
        self,
        *,
        ticket_id: str,
        evidence_root: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_dod_verify",
            "--ticket-id",
            ticket_id,
        ]
        src_path = str(Path(__file__).resolve().parents[4] / "src")
        existing = os.environ.get("PYTHONPATH", "")
        pythonpath = f"{src_path}:{existing}" if existing else src_path
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "PYTHONPATH": pythonpath,
            "HOME": os.environ.get("HOME", ""),
        }
        if evidence_root is not None:
            env["ONEX_EVIDENCE_ROOT"] = str(evidence_root)
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=60,
        )

    def test_no_contract_path_and_undiscoverable_ticket_exits_nonzero(
        self,
    ) -> None:
        """No --contract-path and no discoverable OCC contract (no
        ONEX_CC_REPO_PATH / OMNI_HOME in the subprocess env) -> exit != 0.
        """
        result = self._run_main(ticket_id="OMN-NOEXIST-CLI-15380")

        assert result.returncode != 0, (
            f"expected non-zero exit for a ticket with no discoverable "
            f"contract; got 0\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        state = json.loads(result.stdout)
        assert state["status"] == "skipped"
        assert state.get("error_message", "").startswith("CONTRACT_MISSING")

    def test_no_contract_path_receipt_is_fail_not_pass(self, tmp_path: Path) -> None:
        """When ONEX_EVIDENCE_ROOT is set, the persisted receipt for a
        missing-contract run must be FAIL, not PASS.
        """
        # ModelDodReceipt.ticket_id must match OMN-\d+ — use a numeric-only
        # ticket id that is (deliberately) undiscoverable in this env.
        ticket_id = "OMN-99999380"
        evidence_root = tmp_path / "evidence"

        result = self._run_main(ticket_id=ticket_id, evidence_root=evidence_root)

        assert result.returncode != 0
        receipt_path = evidence_root / ticket_id / "dod_report.json"
        assert receipt_path.exists(), (
            f"expected receipt at {receipt_path}; stdout: {result.stdout}; "
            f"stderr: {result.stderr}"
        )
        body = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert body["status"] == "FAIL", (
            f"a run that verified ZERO checks receipted {body['status']!r} — "
            "must be FAIL"
        )
