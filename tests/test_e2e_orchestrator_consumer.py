# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_e2e_orchestrator consumer wiring (OMN-10465)."""

from __future__ import annotations

import json
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnimarket.nodes.node_e2e_orchestrator.consumer import (
    TOPIC_BUILD_COMPLETED,
    TOPIC_PR_LIFECYCLE_COMPLETED,
    TOPIC_PR_LIFECYCLE_START,
    _check_pr_ci,
    _handle_merge_completed,
    _write_evidence,
)


@pytest.mark.unit
class TestE2EConsumerTopics:
    """Topic constants match contract.yaml declarations."""

    def test_build_completed_topic(self) -> None:
        assert (
            TOPIC_BUILD_COMPLETED
            == "onex.evt.omnimarket.build-loop-orchestrator-completed.v1"
        )

    def test_pr_lifecycle_start_topic(self) -> None:
        assert (
            TOPIC_PR_LIFECYCLE_START
            == "onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1"
        )

    def test_pr_lifecycle_completed_topic(self) -> None:
        assert (
            TOPIC_PR_LIFECYCLE_COMPLETED
            == "onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1"
        )


@pytest.mark.unit
class TestWriteEvidence:
    """_write_evidence writes JSON files under the state dir."""

    def test_writes_json_file(self, tmp_path: pathlib.Path) -> None:

        with patch(
            "omnimarket.nodes.node_e2e_orchestrator.consumer._state_dir",
            return_value=tmp_path,
        ):
            _write_evidence("corr-abc", "build_completed.json", {"status": "ok"})

        out = tmp_path / "e2e-runs" / "corr-abc" / "build_completed.json"
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["status"] == "ok"

    def test_creates_parent_dirs(self, tmp_path: pathlib.Path) -> None:

        with patch(
            "omnimarket.nodes.node_e2e_orchestrator.consumer._state_dir",
            return_value=tmp_path / "nested" / "dirs",
        ):
            _write_evidence("corr-xyz", "test.json", {"x": 1})

        out = tmp_path / "nested" / "dirs" / "e2e-runs" / "corr-xyz" / "test.json"
        assert out.exists()


@pytest.mark.unit
class TestCheckPrCi:
    """_check_pr_ci parses gh pr checks output correctly."""

    def test_unparseable_pr_ref_fails_closed(self) -> None:
        all_green, any_failed, summary = _check_pr_ci("not-a-valid-ref")
        assert all_green is False
        assert any_failed is False
        assert summary["status"] == "unparseable"
        assert summary["error"] == "invalid pr_ref format"

    def test_all_success_checks_returns_green(self) -> None:
        checks_json = json.dumps(
            [
                {"name": "ci/test", "state": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "ci/lint", "state": "COMPLETED", "conclusion": "SUCCESS"},
            ]
        )
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = checks_json

        with patch("subprocess.run", return_value=mock_result):
            all_green, any_failed, summary = _check_pr_ci("OmniNode-ai/omnimarket#42")

        assert all_green is True
        assert any_failed is False
        assert len(summary["checks"]) == 2

    def test_failed_check_returns_not_green_and_failed(self) -> None:
        checks_json = json.dumps(
            [
                {"name": "ci/test", "state": "COMPLETED", "conclusion": "FAILURE"},
            ]
        )
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = checks_json

        with patch("subprocess.run", return_value=mock_result):
            all_green, any_failed, _summary = _check_pr_ci("OmniNode-ai/omnimarket#42")

        assert all_green is False
        assert any_failed is True

    def test_pending_check_returns_not_green_not_failed(self) -> None:
        checks_json = json.dumps(
            [
                {"name": "ci/test", "state": "IN_PROGRESS", "conclusion": ""},
            ]
        )
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = checks_json

        with patch("subprocess.run", return_value=mock_result):
            all_green, _any_failed, _summary = _check_pr_ci("OmniNode-ai/omnimarket#42")

        assert all_green is False

    def test_gh_cli_failure_returns_not_green(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "gh: not authenticated"

        with patch("subprocess.run", return_value=mock_result):
            all_green, _any_failed, summary = _check_pr_ci("OmniNode-ai/omnimarket#42")

        assert all_green is False
        assert "error" in summary


@pytest.mark.unit
class TestHandleMergeCompleted:
    """_handle_merge_completed writes evidence bundle."""

    @pytest.mark.asyncio
    async def test_writes_merge_completed_and_receipts(
        self, tmp_path: pathlib.Path
    ) -> None:
        payload = {
            "correlation_id": "corr-merge-test",
            "prs_merged": 3,
            "prs_fixed": 1,
            "final_state": "COMPLETE",
        }

        with patch(
            "omnimarket.nodes.node_e2e_orchestrator.consumer._state_dir",
            return_value=tmp_path,
        ):
            await _handle_merge_completed(payload)

        run_dir = tmp_path / "e2e-runs" / "corr-merge-test"
        assert (run_dir / "merge_completed.json").exists()
        assert (run_dir / "receipts.json").exists()

        receipts = json.loads((run_dir / "receipts.json").read_text())
        assert receipts["prs_merged"] == 3
        assert receipts["merge_completed"] is True
        assert receipts["ci_green"] is True

    @pytest.mark.asyncio
    async def test_handles_missing_correlation_id(self, tmp_path: pathlib.Path) -> None:
        with patch(
            "omnimarket.nodes.node_e2e_orchestrator.consumer._state_dir",
            return_value=tmp_path,
        ):
            await _handle_merge_completed({})

        run_dir = tmp_path / "e2e-runs" / "unknown"
        assert (run_dir / "receipts.json").exists()


@pytest.mark.unit
class TestHandleBuildCompleted:
    """_handle_build_completed writes initial evidence and triggers sweep when CI green."""

    @pytest.mark.asyncio
    async def test_publishes_pr_lifecycle_start_when_ci_green(
        self, tmp_path: pathlib.Path
    ) -> None:
        from omnimarket.nodes.node_e2e_orchestrator.consumer import (
            _handle_build_completed,
        )

        producer = AsyncMock()
        producer.send_and_wait = AsyncMock()

        payload = {
            "correlation_id": "corr-build-001",
            "pr_refs": [],  # empty — skips CI poll
            "cost_event_keys": [],
        }

        with patch(
            "omnimarket.nodes.node_e2e_orchestrator.consumer._state_dir",
            return_value=tmp_path,
        ):
            await _handle_build_completed(
                payload, producer, ci_timeout=5, ci_interval=1
            )

        # With empty pr_refs, CI poll skips and sweep is triggered
        producer.send_and_wait.assert_awaited_once()
        call_args = producer.send_and_wait.call_args
        assert call_args[0][0] == TOPIC_PR_LIFECYCLE_START
        sweep_cmd = call_args[0][1]
        assert sweep_cmd["correlation_id"] == "corr-build-001"

    @pytest.mark.asyncio
    async def test_writes_run_manifest_and_evidence(
        self, tmp_path: pathlib.Path
    ) -> None:
        from omnimarket.nodes.node_e2e_orchestrator.consumer import (
            _handle_build_completed,
        )

        producer = AsyncMock()
        producer.send_and_wait = AsyncMock()

        payload = {
            "correlation_id": "corr-build-002",
            "pr_refs": [],
            "cost_event_keys": ["key-1", "key-2"],
            "cycles_completed": 1,
        }

        with patch(
            "omnimarket.nodes.node_e2e_orchestrator.consumer._state_dir",
            return_value=tmp_path,
        ):
            await _handle_build_completed(
                payload, producer, ci_timeout=5, ci_interval=1
            )

        run_dir = tmp_path / "e2e-runs" / "corr-build-002"
        assert (run_dir / "run_manifest.json").exists()
        assert (run_dir / "build_completed.json").exists()
        assert (run_dir / "pr_refs.json").exists()
        assert (run_dir / "cost_event_refs.json").exists()

        manifest = json.loads((run_dir / "run_manifest.json").read_text())
        assert manifest["correlation_id"] == "corr-build-002"
        cost_refs = json.loads((run_dir / "cost_event_refs.json").read_text())
        assert cost_refs["cost_event_keys"] == ["key-1", "key-2"]

    @pytest.mark.asyncio
    async def test_does_not_publish_when_ci_fails(self, tmp_path: pathlib.Path) -> None:
        from omnimarket.nodes.node_e2e_orchestrator.consumer import (
            _handle_build_completed,
        )

        producer = AsyncMock()
        producer.send_and_wait = AsyncMock()

        failed_result = MagicMock()
        failed_result.returncode = 0
        failed_result.stdout = json.dumps(
            [{"name": "ci/test", "state": "COMPLETED", "conclusion": "FAILURE"}]
        )

        payload = {
            "correlation_id": "corr-build-ci-fail",
            "pr_refs": ["OmniNode-ai/omnimarket#99"],
            "cost_event_keys": [],
        }

        with (
            patch(
                "omnimarket.nodes.node_e2e_orchestrator.consumer._state_dir",
                return_value=tmp_path,
            ),
            patch("subprocess.run", return_value=failed_result),
        ):
            await _handle_build_completed(
                payload, producer, ci_timeout=5, ci_interval=1
            )

        # CI failed — no merge sweep should be triggered
        producer.send_and_wait.assert_not_awaited()
