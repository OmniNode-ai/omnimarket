# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for scripts/ci/run_dep_health_sweep.py CI gate script."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the scripts/ci directory is importable
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))


@pytest.fixture
def clean_fixture(tmp_path: Path) -> Path:
    """A fixture directory with no findings."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    return tmp_path


@pytest.fixture
def critical_finding_fixture(tmp_path: Path) -> Path:
    """A fixture directory that produces a CRITICAL finding."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    return tmp_path


class TestRunDepHealthSweepScript:
    """Tests for the dep health CI gate script."""

    def test_script_uses_node_public_api(self) -> None:
        """Script must not bypass the node package public API."""
        script = REPO_ROOT / "scripts" / "ci" / "run_dep_health_sweep.py"

        content = script.read_text(encoding="utf-8")

        assert "node_dependency_health_sweep.handlers" not in content

    def test_exit_zero_on_clean_tree(self, clean_fixture: Path) -> None:
        """Script exits 0 when no findings at or above threshold."""
        from run_dep_health_sweep import main

        # Mock the handler to return clean result
        mock_result = MagicMock()
        mock_result.findings = []
        mock_result.status = "clean"
        mock_result.run_id = "test-run-id"
        mock_result.summary = {}
        mock_result.baseline_delta = None
        mock_result.graphify_version = "ast-fallback"
        mock_result.model_dump = MagicMock(
            return_value={
                "status": "clean",
                "run_id": "test-run-id",
                "findings": [],
                "summary": {},
                "baseline_delta": None,
                "graphify_version": "ast-fallback",
            }
        )

        with patch("run_dep_health_sweep.HandlerDepHealthSweep") as mock_handler:
            mock_handler.return_value.handle.return_value = mock_result
            rc = main(
                [
                    "--repo-roots",
                    str(clean_fixture),
                    "--severity-threshold",
                    "MAJOR",
                    "--exit-nonzero-on-findings",
                ]
            )

        assert rc == 0

    def test_exit_one_on_findings_with_flag(
        self, critical_finding_fixture: Path
    ) -> None:
        """Script exits 1 when findings at or above threshold and --exit-nonzero-on-findings is set."""
        from run_dep_health_sweep import main

        mock_finding = MagicMock()
        mock_finding.severity = MagicMock()
        mock_finding.severity.value = "CRITICAL"

        mock_result = MagicMock()
        mock_result.findings = [mock_finding]
        mock_result.status = "findings"
        mock_result.run_id = "test-run-id"
        mock_result.summary = {"MISSING_TOPIC_EDGE": 1}
        mock_result.baseline_delta = None
        mock_result.graphify_version = "ast-fallback"
        mock_result.model_dump = MagicMock(
            return_value={
                "status": "findings",
                "run_id": "test-run-id",
                "findings": [{"severity": "CRITICAL"}],
                "summary": {"MISSING_TOPIC_EDGE": 1},
                "baseline_delta": None,
                "graphify_version": "ast-fallback",
            }
        )

        with patch("run_dep_health_sweep.HandlerDepHealthSweep") as mock_handler:
            mock_handler.return_value.handle.return_value = mock_result
            rc = main(
                [
                    "--repo-roots",
                    str(critical_finding_fixture),
                    "--severity-threshold",
                    "MAJOR",
                    "--exit-nonzero-on-findings",
                ]
            )

        assert rc == 1

    def test_exit_zero_on_findings_without_flag(
        self, critical_finding_fixture: Path
    ) -> None:
        """Script exits 0 when findings exist but --exit-nonzero-on-findings is not set."""
        from run_dep_health_sweep import main

        mock_finding = MagicMock()
        mock_finding.severity = MagicMock()
        mock_finding.severity.value = "CRITICAL"

        mock_result = MagicMock()
        mock_result.findings = [mock_finding]
        mock_result.status = "findings"
        mock_result.run_id = "test-run-id"
        mock_result.summary = {"MISSING_TOPIC_EDGE": 1}
        mock_result.baseline_delta = None
        mock_result.graphify_version = "ast-fallback"
        mock_result.model_dump = MagicMock(
            return_value={
                "status": "findings",
                "run_id": "test-run-id",
                "findings": [{"severity": "CRITICAL"}],
                "summary": {"MISSING_TOPIC_EDGE": 1},
                "baseline_delta": None,
                "graphify_version": "ast-fallback",
            }
        )

        with patch("run_dep_health_sweep.HandlerDepHealthSweep") as mock_handler:
            mock_handler.return_value.handle.return_value = mock_result
            rc = main(
                [
                    "--repo-roots",
                    str(critical_finding_fixture),
                    "--severity-threshold",
                    "MAJOR",
                ]
            )

        assert rc == 0

    def test_output_is_valid_json(
        self, clean_fixture: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Script output is parseable JSON."""
        from run_dep_health_sweep import main

        mock_result = MagicMock()
        mock_result.findings = []
        mock_result.status = "clean"
        mock_result.run_id = "test-run-id"
        mock_result.summary = {}
        mock_result.baseline_delta = None
        mock_result.graphify_version = "ast-fallback"
        mock_result.model_dump = MagicMock(
            return_value={
                "status": "clean",
                "run_id": "test-run-id",
                "findings": [],
                "summary": {},
                "baseline_delta": None,
                "graphify_version": "ast-fallback",
            }
        )

        with patch("run_dep_health_sweep.HandlerDepHealthSweep") as mock_handler:
            mock_handler.return_value.handle.return_value = mock_result
            main(
                [
                    "--repo-roots",
                    str(clean_fixture),
                    "--severity-threshold",
                    "MAJOR",
                ]
            )

        captured = capsys.readouterr()
        # Output should be valid JSON
        parsed = json.loads(captured.out)
        assert isinstance(parsed, dict)
        assert "status" in parsed

    def test_exit_two_on_ast_fallback_failure(self, tmp_path: Path) -> None:
        """Script exits 2 when AST fallback itself fails."""
        from run_dep_health_sweep import main

        with patch("run_dep_health_sweep.HandlerDepHealthSweep") as mock_handler:
            mock_handler.return_value.handle.side_effect = RuntimeError(
                "AST parse failure"
            )
            rc = main(
                [
                    "--repo-roots",
                    str(tmp_path),
                    "--severity-threshold",
                    "MAJOR",
                    "--exit-nonzero-on-findings",
                ]
            )

        assert rc == 2

    def test_below_threshold_findings_exit_zero(self, tmp_path: Path) -> None:
        """Script exits 0 when only findings below threshold exist."""
        from run_dep_health_sweep import main

        mock_finding = MagicMock()
        mock_finding.severity = MagicMock()
        mock_finding.severity.value = "MINOR"  # below MAJOR threshold

        mock_result = MagicMock()
        mock_result.findings = [mock_finding]
        mock_result.status = "findings"
        mock_result.run_id = "test-run-id"
        mock_result.summary = {"DEAD_IMPORT": 1}
        mock_result.baseline_delta = None
        mock_result.graphify_version = "ast-fallback"
        mock_result.model_dump = MagicMock(
            return_value={
                "status": "findings",
                "run_id": "test-run-id",
                "findings": [{"severity": "MINOR"}],
                "summary": {"DEAD_IMPORT": 1},
                "baseline_delta": None,
                "graphify_version": "ast-fallback",
            }
        )

        with patch("run_dep_health_sweep.HandlerDepHealthSweep") as mock_handler:
            mock_handler.return_value.handle.return_value = mock_result
            rc = main(
                [
                    "--repo-roots",
                    str(tmp_path),
                    "--severity-threshold",
                    "MAJOR",
                    "--exit-nonzero-on-findings",
                ]
            )

        assert rc == 0
