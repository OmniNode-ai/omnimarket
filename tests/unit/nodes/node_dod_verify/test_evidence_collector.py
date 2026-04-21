# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for EvidenceCollector — contract loading and check execution."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)


def _write_contract(
    tmp_path: Path,
    ticket_id: str = "OMN-TEST",
    dod_evidence: list[dict] | None = None,
) -> Path:
    """Write a minimal contract YAML and return its path."""
    contract = {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "dod_evidence": dod_evidence or [],
    }
    p = tmp_path / f"{ticket_id}.yaml"
    p.write_text(yaml.dump(contract), encoding="utf-8")
    return p


@pytest.mark.unit
class TestEvidenceCollector:
    """Unit tests for EvidenceCollector."""

    def test_explicit_contract_path(self, tmp_path: Path) -> None:
        """Collector loads contract from explicit path."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "File exists",
                    "checks": [
                        {
                            "check_type": "command",
                            "command": "true",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert len(results) == 1
        assert results[0].evidence_id == "dod-001"
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    def test_missing_contract_file(self, tmp_path: Path) -> None:
        """Collector returns FAILED when contract file doesn't exist."""
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-NOPE",
            contract_path=str(tmp_path / "nonexistent.yaml"),
        )
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "does not exist" in (results[0].message or "").lower()

    def test_empty_dod_evidence(self, tmp_path: Path) -> None:
        """Contract with no dod_evidence -> single SKIPPED result."""
        _write_contract(tmp_path, dod_evidence=[])
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.SKIPPED

    def test_command_check_passes(self, tmp_path: Path) -> None:
        """Command check with exit 0 -> VERIFIED."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "True check",
                    "checks": [{"check_type": "command", "command": "true"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    def test_command_check_fails(self, tmp_path: Path) -> None:
        """Command check with non-zero exit -> FAILED."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "False check",
                    "checks": [{"check_type": "command", "command": "false"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.FAILED

    def test_unsupported_check_type(self, tmp_path: Path) -> None:
        """Unknown check_type -> SKIPPED."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Mystery check",
                    "checks": [{"check_type": "telepathy"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.SKIPPED
        assert "telepathy" in (results[0].message or "")

    def test_no_checks_defined(self, tmp_path: Path) -> None:
        """Evidence item with empty checks list -> SKIPPED."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "No checks",
                    "checks": [],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert results[0].status == EnumEvidenceCheckStatus.SKIPPED

    def test_ticket_id_mismatch(self, tmp_path: Path) -> None:
        """Collector rejects contract whose ticket_id doesn't match."""
        _write_contract(
            tmp_path,
            ticket_id="OMN-OTHER",
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "True check",
                    "checks": [{"check_type": "command", "command": "true"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-WRONG",
            contract_path=str(tmp_path / "OMN-OTHER.yaml"),
        )
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "mismatch" in (results[0].description or "").lower()

    def test_malformed_dod_evidence_not_list(self, tmp_path: Path) -> None:
        """dod_evidence that isn't a list -> FAILED."""
        contract = {
            "schema_version": "1.0.0",
            "ticket_id": "OMN-TEST",
            "dod_evidence": {"bad": True},
        }
        p = tmp_path / "OMN-TEST.yaml"
        p.write_text(yaml.dump(contract), encoding="utf-8")
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(p),
        )
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "must be a list" in (results[0].message or "").lower()

    def test_malformed_checks_not_list(self, tmp_path: Path) -> None:
        """checks that isn't a list -> FAILED for that evidence item."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Bad checks",
                    "checks": "not a list",
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert "must be a list" in (results[0].message or "").lower()

    def test_shell_injection_in_ticket_id(self, tmp_path: Path) -> None:
        """Malicious ticket_id is quoted by shlex.quote and does not alter shell flow.

        The contract ticket_id matches the injected value so the test reaches the
        command-templating path. shlex.quote wraps the shell metacharacters in single
        quotes, turning them into a literal string argument for echo rather than
        executing the injected payload.  The command exits 0 and the result is
        VERIFIED, confirming that the injection was neutralised rather than executed.
        """
        malicious_ticket_id = "OMN-TEST; false"
        contract_path = _write_contract(
            tmp_path,
            ticket_id=malicious_ticket_id,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Quoted substitution",
                    "checks": [
                        {"check_type": "command", "command": "echo {ticket_id}"}
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            malicious_ticket_id,
            contract_path=str(contract_path),
        )
        # shlex.quote turns "OMN-TEST; false" into a single-quoted shell literal so
        # the semicolon is NOT interpreted as a command separator.  echo exits 0.
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    def test_multiple_evidence_items(self, tmp_path: Path) -> None:
        """Multiple dod_evidence items produce multiple results."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Pass",
                    "checks": [{"check_type": "command", "command": "true"}],
                },
                {
                    "id": "dod-002",
                    "description": "Fail",
                    "checks": [{"check_type": "command", "command": "false"}],
                },
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert len(results) == 2
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
        assert results[1].status == EnumEvidenceCheckStatus.FAILED


@pytest.mark.unit
class TestHandlerWithCollector:
    """Integration tests: handler auto-collects evidence when not provided."""

    def test_handler_collects_from_contract(self, tmp_path: Path) -> None:
        """Handler with no evidence_results calls collector."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "True check",
                    "checks": [{"check_type": "command", "command": "true"}],
                }
            ],
        )
        handler = HandlerDodVerify()
        result = handler.handle(
            {
                "correlation_id": str(uuid4()),
                "ticket_id": "OMN-TEST",
                "contract_path": str(tmp_path / "OMN-TEST.yaml"),
                "dry_run": False,
                "requested_at": datetime.now(tz=UTC).isoformat(),
            }
        )
        assert isinstance(result, dict)
        assert result["status"] == "verified"
        assert result["total_checks"] == 1
        assert result["verified_count"] == 1

    def test_handler_pre_provided_results_still_work(self, tmp_path: Path) -> None:
        """Pre-provided evidence_results bypass collector (backward compat)."""
        from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
            ModelDodVerifyStartCommand,
        )
        from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
            ModelEvidenceCheckResult,
        )

        handler = HandlerDodVerify()
        cmd = ModelDodVerifyStartCommand(
            correlation_id=uuid4(),
            ticket_id="OMN-TEST",
            dry_run=False,
            requested_at=datetime.now(tz=UTC),
        )
        checks = [
            ModelEvidenceCheckResult(
                evidence_id="dod-001",
                description="Pass",
                status=EnumEvidenceCheckStatus.VERIFIED,
            ),
        ]
        state = handler._handle_typed(cmd, evidence_results=checks)
        assert state.status == EnumDodVerifyStatus.VERIFIED
        assert state.verified_count == 1

    def test_handler_no_contract_skipped(self) -> None:
        """Handler returns SKIPPED when contract can't be found."""
        handler = HandlerDodVerify()
        result = handler.handle(
            {
                "correlation_id": str(uuid4()),
                "ticket_id": "OMN-NOEXIST-99999",
                "dry_run": False,
                "requested_at": datetime.now(tz=UTC).isoformat(),
            }
        )
        assert isinstance(result, dict)
        assert result["status"] == "skipped"
