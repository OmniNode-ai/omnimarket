# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for ReceiptWriter.

Covers:
- ReceiptWriter.from_env() raises KeyError when ONEX_EVIDENCE_ROOT is unset.
- write() creates the file with correct provenance fields.
- write() creates parent directories automatically.
- Atomic write (tmp-then-rename) leaves no .tmp file on success.
- resolve_path() returns the canonical per-ticket path.
- Explicit output_path overrides the canonical path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_dod_verify.models.model_dod_report_receipt import (
    EnumReceiptGenerator,
    ModelDodReportReceipt,
    ModelDodReportResult,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
)
from omnimarket.nodes.node_dod_verify.receipt_writer import (
    ModelReceiptWriterConfig,
    ReceiptWriter,
)


def _make_receipt(ticket_id: str = "OMN-9999") -> ModelDodReportReceipt:
    return ModelDodReportReceipt(
        timestamp=datetime.now(tz=UTC),
        ticket_id=ticket_id,
        generator_version="1.1.0",
        node_correlation_id=uuid4(),
        result=ModelDodReportResult(
            total=2,
            verified=2,
            failed=0,
            skipped=0,
            status=EnumDodVerifyStatus.VERIFIED,
        ),
        checks=[],
    )


@pytest.mark.unit
class TestReceiptWriterFromEnv:
    def test_raises_key_error_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ReceiptWriter.from_env() must fail-fast when ONEX_EVIDENCE_ROOT is unset."""
        monkeypatch.delenv("ONEX_EVIDENCE_ROOT", raising=False)
        with pytest.raises(KeyError):
            ReceiptWriter.from_env()

    def test_constructs_from_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """from_env() reads ONEX_EVIDENCE_ROOT and builds config."""
        monkeypatch.setenv("ONEX_EVIDENCE_ROOT", str(tmp_path))
        writer = ReceiptWriter.from_env()
        assert writer._config.evidence_root == tmp_path


@pytest.mark.unit
class TestReceiptWriterWrite:
    def _writer(self, tmp_path: Path) -> ReceiptWriter:
        config = ModelReceiptWriterConfig(evidence_root=tmp_path)
        return ReceiptWriter(config)

    def test_writes_file_at_canonical_path(self, tmp_path: Path) -> None:
        """write() creates the file at evidence_root/ticket_id/dod_report.json."""
        writer = self._writer(tmp_path)
        receipt = _make_receipt("OMN-1234")
        written = writer.write(receipt)
        assert written == tmp_path / "OMN-1234" / "dod_report.json"
        assert written.exists()

    def test_generated_by_field_correct(self, tmp_path: Path) -> None:
        """Written receipt must have generated_by=node_dod_verify."""
        writer = self._writer(tmp_path)
        receipt = _make_receipt("OMN-1234")
        written = writer.write(receipt)
        data = json.loads(written.read_text())
        assert data["generated_by"] == EnumReceiptGenerator.NODE_DOD_VERIFY

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        """write() creates nested parent directories that don't exist yet."""
        writer = self._writer(tmp_path)
        receipt = _make_receipt("OMN-5555")
        written = writer.write(receipt)
        assert written.parent.is_dir()

    def test_explicit_output_path(self, tmp_path: Path) -> None:
        """When output_path is provided it overrides the canonical path."""
        writer = self._writer(tmp_path)
        receipt = _make_receipt("OMN-1234")
        custom = tmp_path / "custom" / "output.json"
        written = writer.write(receipt, output_path=custom)
        assert written == custom
        assert written.exists()

    def test_no_tmp_file_remains_after_atomic_write(self, tmp_path: Path) -> None:
        """Atomic write leaves no .tmp siblings after success."""
        writer = self._writer(tmp_path)
        receipt = _make_receipt("OMN-1234")
        writer.write(receipt)
        parent = tmp_path / "OMN-1234"
        tmp_files = list(parent.glob("*.tmp"))
        assert tmp_files == [], f"Unexpected .tmp files: {tmp_files}"

    def test_receipt_is_valid_json(self, tmp_path: Path) -> None:
        """Written file must parse as valid JSON."""
        writer = self._writer(tmp_path)
        receipt = _make_receipt("OMN-7777")
        written = writer.write(receipt)
        data = json.loads(written.read_text())
        assert data["ticket_id"] == "OMN-7777"
        assert data["schema_version"] == "1.0.0"
        assert "node_correlation_id" in data

    def test_resolve_path_returns_canonical(self, tmp_path: Path) -> None:
        """resolve_path() returns evidence_root/ticket_id/dod_report.json."""
        writer = self._writer(tmp_path)
        assert (
            writer.resolve_path("OMN-9001") == tmp_path / "OMN-9001" / "dod_report.json"
        )

    def test_non_atomic_write(self, tmp_path: Path) -> None:
        """Non-atomic write still produces the correct file."""
        config = ModelReceiptWriterConfig(evidence_root=tmp_path, atomic_write=False)
        writer = ReceiptWriter(config)
        receipt = _make_receipt("OMN-2222")
        written = writer.write(receipt)
        assert written.exists()
        data = json.loads(written.read_text())
        assert data["generated_by"] == "node_dod_verify"
