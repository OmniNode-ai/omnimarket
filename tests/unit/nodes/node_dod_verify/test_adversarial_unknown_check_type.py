# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Adversarial tests for unknown check_type handling in EvidenceCollector.

These tests guard against the silent-skip footgun fixed in OMN-9571:
unknown check_type values must return FAILED, not SKIPPED, so that
DoD evidence is never silently bypassed by a misspelled or unregistered
check type.

A test here FAILS against the pre-OMN-9571-fix behavior (SKIPPED on unknown
check_type). If you revert the fix and run this suite, these tests will fail —
that is the intended behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
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
    contract = {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "dod_evidence": dod_evidence or [],
    }
    p = tmp_path / f"{ticket_id}.yaml"
    p.write_text(yaml.dump(contract), encoding="utf-8")
    return p


@pytest.mark.unit
class TestUnknownCheckTypeAdversarial:
    """Unknown check_type must FAIL, not SKIPPED.

    Pre-condition that validates the guard: reverting evidence_collector.py to
    return SKIPPED for unknown check_type would cause every test in this class
    to fail, proving the tests catch the original OMN-9571 bug class.
    """

    def test_unknown_check_type_returns_failed(self, tmp_path: Path) -> None:
        """Completely fabricated check_type must return FAILED, not SKIPPED."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Uses a totally made-up check type",
                    "checks": [{"check_type": "totally_made_up"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert len(results) == 1
        result = results[0]
        # MUST be FAILED — SKIPPED is the pre-fix silent-pass bug
        assert result.status == EnumEvidenceCheckStatus.FAILED, (
            f"Expected FAILED for unknown check_type, got {result.status}. "
            "This is the OMN-9571 silent-skip footgun."
        )
        # Error message must identify the unrecognised type
        assert "totally_made_up" in (result.message or ""), (
            f"Error message must name the unknown check_type, got: {result.message!r}"
        )

    def test_typo_check_type_returns_failed(self, tmp_path: Path) -> None:
        """Common footgun: 'file_exits' (typo for 'file_exists') must FAIL, not SKIPPED.

        This is the highest-probability real-world trigger of the silent-skip bug.
        A one-character transposition silently bypassed all DoD evidence checks.
        """
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Typo: file_exits instead of file_exists",
                    "checks": [{"check_type": "file_exits", "path": "some/file.txt"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert len(results) == 1
        result = results[0]
        assert result.status == EnumEvidenceCheckStatus.FAILED, (
            f"Expected FAILED for typo check_type 'file_exits', got {result.status}. "
            "Typos must not silently pass DoD checks."
        )
        assert "file_exits" in (result.message or ""), (
            f"Error message must name the typo'd check_type, got: {result.message!r}"
        )

    def test_empty_string_check_type_returns_failed(self, tmp_path: Path) -> None:
        """Empty string check_type must FAIL, not SKIPPED."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Empty check_type",
                    "checks": [{"check_type": ""}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.FAILED, (
            "Empty check_type must FAIL, not SKIPPED."
        )

    def test_missing_check_type_key_returns_failed(self, tmp_path: Path) -> None:
        """Check dict with no check_type key at all must FAIL, not SKIPPED."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "No check_type key",
                    "checks": [{"command": "true"}],  # missing check_type entirely
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.FAILED, (
            "Missing check_type key must FAIL — silence is not acceptable."
        )

    def test_known_command_check_type_still_works(self, tmp_path: Path) -> None:
        """Sanity: the 'command' check_type still returns VERIFIED for exit-0 commands."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Known good command",
                    "checks": [{"check_type": "command", "command": "true"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    def test_known_file_exists_check_type_still_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sanity: the 'file_exists' check_type (added in OMN-9571) still works."""
        target = tmp_path / "artifact.txt"
        target.write_text("ok", encoding="utf-8")
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "File artifact present",
                    "checks": [{"check_type": "file_exists", "path": "artifact.txt"}],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-TEST",
            contract_path=str(tmp_path / "OMN-TEST.yaml"),
        )
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    def test_multiple_checks_unknown_type_fails_item(self, tmp_path: Path) -> None:
        """If a check list has a valid check followed by an unknown type, item must FAIL."""
        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "Mixed valid + invalid check types",
                    "checks": [
                        {"check_type": "command", "command": "true"},
                        {"check_type": "nonexistent_validator"},
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
        assert results[0].status == EnumEvidenceCheckStatus.FAILED, (
            "Unknown check_type in a multi-check item must cause the item to FAIL."
        )
