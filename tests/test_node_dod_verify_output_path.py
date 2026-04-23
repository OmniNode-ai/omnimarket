# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_dod_verify --output-path path (run_and_persist).

Covers:
- run_and_persist() writes receipt with generated_by=node_dod_verify.
- receipt carries correct ticket_id and generator_version.
- completed_event.receipt_path matches the written path.
- stdout path (run_verification) is unaffected (no file written).
- all-skipped receipts carry result.status=skipped.
- completed_event.schema_version is "1.1.0".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.receipt_writer import (
    ModelReceiptWriterConfig,
    ReceiptWriter,
)


def _command(ticket_id: str = "OMN-9524") -> ModelDodVerifyStartCommand:
    return ModelDodVerifyStartCommand(
        correlation_id=uuid4(),
        ticket_id=ticket_id,
        dry_run=False,
        requested_at=datetime.now(tz=UTC),
    )


def _writer(evidence_root: Path) -> ReceiptWriter:
    return ReceiptWriter(ModelReceiptWriterConfig(evidence_root=evidence_root))


def _checks_verified() -> list[ModelEvidenceCheckResult]:
    return [
        ModelEvidenceCheckResult(
            evidence_id="dod-001",
            description="Tests pass",
            status=EnumEvidenceCheckStatus.VERIFIED,
        ),
        ModelEvidenceCheckResult(
            evidence_id="dod-002",
            description="PR merged",
            status=EnumEvidenceCheckStatus.VERIFIED,
        ),
    ]


def _checks_skipped() -> list[ModelEvidenceCheckResult]:
    return [
        ModelEvidenceCheckResult(
            evidence_id="dod-001",
            description="No contract found",
            status=EnumEvidenceCheckStatus.SKIPPED,
        ),
    ]


@pytest.mark.unit
class TestRunAndPersist:
    def test_writes_receipt_with_provenance(self, tmp_path: Path) -> None:
        """run_and_persist() writes dod_report.json with generated_by=node_dod_verify."""
        handler = HandlerDodVerify()
        cmd = _command()
        writer = _writer(tmp_path)

        _state, _event, written_path = handler.run_and_persist(
            cmd, writer, evidence_results=_checks_verified()
        )

        assert written_path.exists()
        data = json.loads(written_path.read_text())
        assert data["generated_by"] == "node_dod_verify"

    def test_receipt_ticket_id_matches(self, tmp_path: Path) -> None:
        """Receipt ticket_id must match the command ticket_id."""
        handler = HandlerDodVerify()
        cmd = _command("OMN-8888")
        writer = _writer(tmp_path)

        _state, _event, written_path = handler.run_and_persist(
            cmd, writer, evidence_results=_checks_verified()
        )

        data = json.loads(written_path.read_text())
        assert data["ticket_id"] == "OMN-8888"

    def test_receipt_generator_version_is_1_1_0(self, tmp_path: Path) -> None:
        """Receipt generator_version must be 1.1.0 (the version that ships ReceiptWriter)."""
        handler = HandlerDodVerify()
        cmd = _command()
        writer = _writer(tmp_path)

        _state, _event, written_path = handler.run_and_persist(
            cmd, writer, evidence_results=_checks_verified()
        )

        data = json.loads(written_path.read_text())
        assert data["generator_version"] == "1.1.0"

    def test_completed_event_receipt_path_matches_written(self, tmp_path: Path) -> None:
        """ModelDodVerifyCompletedEvent.receipt_path must equal the written path."""
        handler = HandlerDodVerify()
        cmd = _command()
        writer = _writer(tmp_path)

        _state, event, written_path = handler.run_and_persist(
            cmd, writer, evidence_results=_checks_verified()
        )

        assert event.receipt_path == written_path

    def test_completed_event_schema_version_is_1_1_0(self, tmp_path: Path) -> None:
        """ModelDodVerifyCompletedEvent.schema_version must be 1.1.0."""
        handler = HandlerDodVerify()
        cmd = _command()
        writer = _writer(tmp_path)

        _state, event, _path = handler.run_and_persist(
            cmd, writer, evidence_results=_checks_verified()
        )

        assert event.schema_version == "1.1.0"

    def test_explicit_output_path_used(self, tmp_path: Path) -> None:
        """When output_path is provided, receipt is written there (not canonical path)."""
        handler = HandlerDodVerify()
        cmd = _command()
        writer = _writer(tmp_path)
        custom = tmp_path / "custom" / "receipt.json"

        _state, event, written_path = handler.run_and_persist(
            cmd, writer, output_path=custom, evidence_results=_checks_verified()
        )

        assert written_path == custom
        assert custom.exists()
        assert event.receipt_path == custom

    def test_all_skipped_receipt_status_is_skipped(self, tmp_path: Path) -> None:
        """All-skipped run must produce receipt with result.status=skipped (not verified)."""
        handler = HandlerDodVerify()
        cmd = _command()
        writer = _writer(tmp_path)

        state, _event, written_path = handler.run_and_persist(
            cmd, writer, evidence_results=_checks_skipped()
        )

        assert state.status == EnumDodVerifyStatus.SKIPPED
        data = json.loads(written_path.read_text())
        assert data["result"]["status"] == "skipped"
        assert data["result"]["failed"] == 0

    def test_stdout_path_unaffected_no_file_written(self, tmp_path: Path) -> None:
        """run_verification() (stdout path) must not write any files."""
        handler = HandlerDodVerify()
        cmd = _command()

        _state, event = handler.run_verification(cmd, _checks_verified())

        # No receipt_path set on the event
        assert event.receipt_path is None
        # No files written under any evidence path
        assert list(tmp_path.rglob("*.json")) == []

    def test_node_correlation_id_present_in_receipt(self, tmp_path: Path) -> None:
        """Receipt must carry node_correlation_id that matches the command correlation_id."""
        handler = HandlerDodVerify()
        cmd = _command()
        writer = _writer(tmp_path)

        _state, _event, written_path = handler.run_and_persist(
            cmd, writer, evidence_results=_checks_verified()
        )

        data = json.loads(written_path.read_text())
        assert data["node_correlation_id"] == str(cmd.correlation_id)

    def test_checks_count_consistent(self, tmp_path: Path) -> None:
        """Receipt result.total == verified + failed + skipped."""
        handler = HandlerDodVerify()
        cmd = _command()
        writer = _writer(tmp_path)

        _state, _event, written_path = handler.run_and_persist(
            cmd, writer, evidence_results=_checks_verified()
        )

        data = json.loads(written_path.read_text())
        r = data["result"]
        assert r["total"] == r["verified"] + r["failed"] + r["skipped"]
