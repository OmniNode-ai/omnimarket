# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for node_dod_verify __main__ receipt write path.

OMN-10046 — when ONEX_EVIDENCE_ROOT is set (or --output-path is provided),
``python -m omnimarket.nodes.node_dod_verify`` MUST write a dod_report.json
file in addition to printing the state JSON to stdout. Without this, Hook 2
(pre_tool_use_dod_completion_guard.sh) cannot find a receipt and blocks the
Done transition for tickets whose code already merged.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _write_contract(
    tmp_path: Path,
    ticket_id: str = "OMN-TEST-MAIN",
    dod_evidence: list[dict] | None = None,
) -> Path:
    """Write a minimal contract YAML and return its path."""
    contract = {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "dod_evidence": dod_evidence
        or [
            {
                "id": "dod-001",
                "description": "trivially true",
                "checks": [{"check_type": "command", "command": "true"}],
            }
        ],
    }
    p = tmp_path / f"{ticket_id}.yaml"
    p.write_text(yaml.dump(contract), encoding="utf-8")
    return p


def _run_main(
    *,
    ticket_id: str,
    contract_path: Path,
    evidence_root: Path | None,
    output_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the node CLI as a subprocess and return the completed process."""
    cmd = [
        sys.executable,
        "-m",
        "omnimarket.nodes.node_dod_verify",
        "--ticket-id",
        ticket_id,
        "--contract-path",
        str(contract_path),
    ]
    if output_path is not None:
        cmd.extend(["--output-path", str(output_path)])
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "PYTHONPATH": str(Path(__file__).resolve().parents[5] / "src"),
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


@pytest.mark.unit
class TestMainWritesReceipt:
    """OMN-10046 — verify __main__ persists dod_report.json to disk."""

    def test_evidence_root_env_writes_canonical_receipt(self, tmp_path: Path) -> None:
        """ONEX_EVIDENCE_ROOT set -> writes <root>/<ticket_id>/dod_report.json.

        This is the contract Hook 2 (pre_tool_use_dod_completion_guard.sh) relies
        on. Without it, every dod_verify run leaves no on-disk artifact and Hook 2
        refuses to allow a Done transition.
        """
        ticket_id = "OMN-TEST-MAIN"
        contract = _write_contract(tmp_path, ticket_id=ticket_id)
        evidence_root = tmp_path / "evidence"

        result = _run_main(
            ticket_id=ticket_id,
            contract_path=contract,
            evidence_root=evidence_root,
        )

        assert result.returncode == 0, (
            f"main exited {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        receipt = evidence_root / ticket_id / "dod_report.json"
        assert receipt.exists(), (
            f"Expected receipt at {receipt}; stdout: {result.stdout};"
            f" stderr: {result.stderr}"
        )

        body = json.loads(receipt.read_text(encoding="utf-8"))
        assert body["ticket_id"] == ticket_id
        # Hook 2 (pre_tool_use_dod_completion_guard.sh) reads result.failed and
        # the timestamp field. The receipt MUST match that schema or the hook
        # blocks Done transitions even on PASS receipts.
        assert "timestamp" in body
        assert body["result"]["failed"] == 0
        assert body["result"]["verified"] >= 1

    def test_explicit_output_path_writes_there(self, tmp_path: Path) -> None:
        """--output-path overrides the env-derived path."""
        ticket_id = "OMN-TEST-MAIN-OUT"
        contract = _write_contract(tmp_path, ticket_id=ticket_id)
        explicit = tmp_path / "custom" / "report.json"

        result = _run_main(
            ticket_id=ticket_id,
            contract_path=contract,
            evidence_root=None,
            output_path=explicit,
        )

        assert result.returncode == 0, (
            f"main exited {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert explicit.exists()
        body = json.loads(explicit.read_text(encoding="utf-8"))
        assert body["ticket_id"] == ticket_id
        assert "timestamp" in body
        assert body["result"]["failed"] == 0

    def test_no_evidence_root_and_no_output_path_does_not_write(
        self, tmp_path: Path
    ) -> None:
        """When neither ONEX_EVIDENCE_ROOT nor --output-path is set, no file written.

        Preserves backward-compat: legacy callers that only consume stdout JSON
        keep working without surprise filesystem side effects.
        """
        ticket_id = "OMN-TEST-MAIN-STDOUT-ONLY"
        contract = _write_contract(tmp_path, ticket_id=ticket_id)

        result = _run_main(
            ticket_id=ticket_id,
            contract_path=contract,
            evidence_root=None,
        )

        assert result.returncode == 0
        # No stray dod_report.json files anywhere under tmp_path.
        strays = list(tmp_path.rglob("dod_report.json"))
        assert strays == [], f"unexpected receipt files: {strays}"

    def test_test_passes_check_writes_verified_receipt(self, tmp_path: Path) -> None:
        """End-to-end OMN-10046 acceptance: test_passes contract -> PASS receipt.

        Mirrors the production flow that produced FAIL receipts for OMN-9866 etc.
        Asserts both that the check runs (no Unknown check_type message) and that
        the receipt on disk is VERIFIED.
        """
        ticket_id = "OMN-TEST-MAIN-TESTPASSES"
        contract = _write_contract(
            tmp_path,
            ticket_id=ticket_id,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "test_passes runs",
                    "checks": [
                        {"check_type": "test_passes", "check_value": "true"},
                    ],
                }
            ],
        )
        evidence_root = tmp_path / "evidence"

        result = _run_main(
            ticket_id=ticket_id,
            contract_path=contract,
            evidence_root=evidence_root,
        )

        assert result.returncode == 0, (
            f"main exited {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        receipt_path = evidence_root / ticket_id / "dod_report.json"
        assert receipt_path.exists()
        body = json.loads(receipt_path.read_text(encoding="utf-8"))
        # Hook 2 only checks result.failed; assert that surface plus the
        # human-readable detail nested inside result.details (so a check
        # message mentioning "Unknown check_type" cannot sneak past).
        assert body["result"]["failed"] == 0
        for detail in body["result"].get("details", []):
            assert "Unknown check_type" not in (detail.get("message") or "")
