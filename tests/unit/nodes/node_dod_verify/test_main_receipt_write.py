# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for node_dod_verify __main__ receipt write path.

OMN-10046, OMN-12403 — when ONEX_EVIDENCE_ROOT is set (or --output-path is
provided), ``python -m omnimarket.nodes.node_dod_verify`` MUST write a
ModelDodReceipt-shaped dod_report.json. The DoD completion guard
(pre_tool_use_dod_completion_guard.sh) requires ``run_timestamp`` and
``status == PASS``; the legacy ``timestamp``/``result`` schema is rejected.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt


def _write_contract(
    tmp_path: Path,
    ticket_id: str = "OMN-10046",
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
    # Prepend project src/ to PYTHONPATH so the worktree's omnimarket package is
    # found; omnibase_core and other deps are resolved via the venv site-packages.
    src_path = str(Path(__file__).resolve().parents[4] / "src")
    import os as _os

    existing = _os.environ.get("PYTHONPATH", "")
    pythonpath = f"{src_path}:{existing}" if existing else src_path
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "PYTHONPATH": pythonpath,
        "HOME": _os.environ.get("HOME", ""),
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
    """OMN-10046, OMN-12403 — verify __main__ persists ModelDodReceipt-shaped dod_report.json."""

    def test_evidence_root_env_writes_canonical_receipt(self, tmp_path: Path) -> None:
        """ONEX_EVIDENCE_ROOT set -> writes <root>/<ticket_id>/dod_report.json.

        Receipt must be ModelDodReceipt-shaped (OMN-12403): has run_timestamp,
        no top-level result key, status == PASS or ADVISORY.
        """
        ticket_id = "OMN-10046"
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

        receipt_path = evidence_root / ticket_id / "dod_report.json"
        assert receipt_path.exists(), (
            f"Expected receipt at {receipt_path}; stdout: {result.stdout};"
            f" stderr: {result.stderr}"
        )

        body = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert body["ticket_id"] == ticket_id
        # ModelDodReceipt shape (OMN-12403): must have run_timestamp, no top-level result
        assert "run_timestamp" in body, (
            "receipt missing run_timestamp (legacy schema rejected by guard)"
        )
        assert "result" not in body, (
            "receipt must not have top-level 'result' key (legacy schema)"
        )
        assert body["status"] in ("PASS", "ADVISORY")
        # Validate the full ModelDodReceipt schema
        ModelDodReceipt.model_validate(body)

    def test_explicit_output_path_writes_there(self, tmp_path: Path) -> None:
        """--output-path overrides the env-derived path; receipt is ModelDodReceipt-shaped."""
        ticket_id = "OMN-10047"
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
        assert "run_timestamp" in body
        assert "result" not in body
        ModelDodReceipt.model_validate(body)

    def test_no_evidence_root_and_no_output_path_does_not_write(
        self, tmp_path: Path
    ) -> None:
        """When neither ONEX_EVIDENCE_ROOT nor --output-path is set, no file written.

        Preserves backward-compat: legacy callers that only consume stdout JSON
        keep working without surprise filesystem side effects.
        """
        ticket_id = "OMN-10048"
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

    def test_test_passes_check_writes_model_dod_receipt(self, tmp_path: Path) -> None:
        """OMN-12403: test_passes contract -> ModelDodReceipt-shaped PASS receipt.

        Validates both that the check runs and that the emitted receipt
        satisfies the DoD guard's accept conditions:
        - has run_timestamp (not legacy timestamp)
        - no top-level result key
        - status == PASS or ADVISORY
        - validates as ModelDodReceipt
        """
        ticket_id = "OMN-12403"
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
        # Guard accept conditions: run_timestamp present, no top-level result, status PASS/ADVISORY
        assert "run_timestamp" in body
        assert "result" not in body
        assert body["status"] in ("PASS", "ADVISORY")
        # probe_stdout contains the check details — no Unknown check_type message
        assert "Unknown check_type" not in body.get("probe_stdout", "")
        # Full schema validation
        ModelDodReceipt.model_validate(body)
