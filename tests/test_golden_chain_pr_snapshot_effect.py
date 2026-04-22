# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_pr_snapshot_effect.

Verifies the effect node: PR parsing, multi-repo aggregation, partial
failure isolation, draft filtering, CI pass/fail detection, label
extraction, and ModelPRInfo compatibility with ModelMergeSweepRequest.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    ModelMergeSweepRequest,
    ModelPRInfo,
)
from omnimarket.nodes.node_pr_snapshot_effect.handlers.handler_pr_snapshot import (
    HandlerPrSnapshot,
    _checks_pass,
    _extract_labels,
    _parse_pr,
)
from omnimarket.nodes.node_pr_snapshot_effect.models.model_pr_snapshot_input import (
    DEFAULT_REPOS,
    ModelPrSnapshotInput,
)
from omnimarket.nodes.node_pr_snapshot_effect.models.model_pr_snapshot_result import (
    ModelPrSnapshotResult,
)


def _make_gh_pr_json(
    number: int = 1,
    title: str = "Test PR",
    mergeable: str = "MERGEABLE",
    merge_state_status: str = "CLEAN",
    is_draft: bool = False,
    review_decision: str | None = "APPROVED",
    status_checks: list[dict[str, str]] | None = None,
    labels: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Build a raw gh pr list JSON object."""
    return {
        "number": number,
        "title": title,
        "mergeable": mergeable,
        "mergeStateStatus": merge_state_status,
        "isDraft": is_draft,
        "reviewDecision": review_decision,
        "statusCheckRollup": status_checks or [{"conclusion": "SUCCESS"}],
        "labels": labels or [],
    }


def _mock_subprocess_run(
    stdout_data: list[dict[str, object]],
    returncode: int = 0,
    stderr: str = "",
) -> MagicMock:
    """Create a mock subprocess.run result."""
    mock_result = MagicMock()
    mock_result.returncode = returncode
    mock_result.stdout = json.dumps(stdout_data)
    mock_result.stderr = stderr
    return mock_result


@pytest.mark.unit
class TestChecksPass:
    """Unit tests for _checks_pass helper."""

    def test_empty_rollup_passes(self) -> None:
        assert _checks_pass([]) is True

    def test_all_success_passes(self) -> None:
        rollup = [
            {"conclusion": "SUCCESS"},
            {"state": "SUCCESS"},
        ]
        assert _checks_pass(rollup) is True

    def test_failure_fails(self) -> None:
        rollup = [
            {"conclusion": "SUCCESS"},
            {"conclusion": "FAILURE"},
        ]
        assert _checks_pass(rollup) is False

    def test_pending_fails(self) -> None:
        rollup = [{"conclusion": "PENDING"}]
        assert _checks_pass(rollup) is False


@pytest.mark.unit
class TestExtractLabels:
    """Unit tests for _extract_labels helper."""

    def test_extracts_names(self) -> None:
        raw = [{"name": "bug"}, {"name": "urgent"}]
        assert _extract_labels(raw) == ["bug", "urgent"]

    def test_skips_empty_names(self) -> None:
        raw = [{"name": "bug"}, {"name": ""}, {"other": "val"}]
        assert _extract_labels(raw) == ["bug"]

    def test_empty_list(self) -> None:
        assert _extract_labels([]) == []


@pytest.mark.unit
class TestParsePr:
    """Unit tests for _parse_pr helper."""

    def test_basic_parse(self) -> None:
        raw = _make_gh_pr_json(number=42, title="feat: add thing")
        pr = _parse_pr(raw, "OmniNode-ai/omniclaude")

        assert pr.number == 42
        assert pr.title == "feat: add thing"
        assert pr.repo == "OmniNode-ai/omniclaude"
        assert pr.mergeable == "MERGEABLE"
        assert pr.merge_state_status == "CLEAN"
        assert pr.is_draft is False
        assert pr.review_decision == "APPROVED"
        assert pr.required_checks_pass is True

    def test_draft_detection(self) -> None:
        raw = _make_gh_pr_json(is_draft=True)
        pr = _parse_pr(raw, "OmniNode-ai/test")
        assert pr.is_draft is True

    def test_ci_failure_detection(self) -> None:
        raw = _make_gh_pr_json(
            status_checks=[{"conclusion": "FAILURE"}],
        )
        pr = _parse_pr(raw, "OmniNode-ai/test")
        assert pr.required_checks_pass is False

    def test_label_extraction(self) -> None:
        raw = _make_gh_pr_json(
            labels=[{"name": "auto-merge"}, {"name": "P0"}],
        )
        pr = _parse_pr(raw, "OmniNode-ai/test")
        assert pr.labels == ["auto-merge", "P0"]

    def test_missing_fields_use_defaults(self) -> None:
        raw = {"number": 1}
        pr = _parse_pr(raw, "OmniNode-ai/test")
        assert pr.title == ""
        assert pr.mergeable == "UNKNOWN"
        assert pr.merge_state_status == "UNKNOWN"
        assert pr.is_draft is False
        assert pr.review_decision is None
        assert pr.required_checks_pass is True
        assert pr.labels == []


@pytest.mark.unit
class TestHandlerPrSnapshotGoldenChain:
    """Golden chain: subprocess mock -> handler -> result -> ModelMergeSweepRequest."""

    def test_single_repo_scan(self) -> None:
        """Scan a single repo and verify PR parsing."""
        pr_data = [_make_gh_pr_json(number=10, title="fix: bug")]
        input_model = ModelPrSnapshotInput(
            repos=("OmniNode-ai/omniclaude",),
            limit_per_repo=50,
        )

        with patch("subprocess.run", return_value=_mock_subprocess_run(pr_data)):
            handler = HandlerPrSnapshot()
            result = handler.handle(input_model)

        assert isinstance(result, ModelPrSnapshotResult)
        assert len(result.repo_results) == 1
        assert result.repo_results[0].success is True
        assert result.total_prs == 1
        assert result.all_prs[0].number == 10
        assert result.all_prs[0].title == "fix: bug"

    def test_multi_repo_aggregation(self) -> None:
        """Scan multiple repos and verify all_prs aggregates across repos."""
        repo_a_prs = [_make_gh_pr_json(number=1), _make_gh_pr_json(number=2)]
        repo_b_prs = [_make_gh_pr_json(number=3)]

        call_count = 0

        def mock_run(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_subprocess_run(repo_a_prs)
            return _mock_subprocess_run(repo_b_prs)

        input_model = ModelPrSnapshotInput(
            repos=("OmniNode-ai/repo_a", "OmniNode-ai/repo_b"),
        )

        with patch("subprocess.run", side_effect=mock_run):
            result = HandlerPrSnapshot().handle(input_model)

        assert result.total_prs == 3
        assert len(result.all_prs) == 3
        assert {pr.number for pr in result.all_prs} == {1, 2, 3}

    def test_partial_failure_one_repo_fails(self) -> None:
        """One repo fails, others succeed — partial failure isolation."""
        good_prs = [_make_gh_pr_json(number=5)]

        call_count = 0

        def mock_run(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_subprocess_run(good_prs)
            return _mock_subprocess_run([], returncode=1, stderr="not found")

        input_model = ModelPrSnapshotInput(
            repos=("OmniNode-ai/good_repo", "OmniNode-ai/bad_repo"),
        )

        with patch("subprocess.run", side_effect=mock_run):
            result = HandlerPrSnapshot().handle(input_model)

        assert result.total_prs == 1
        assert len(result.failed_repos) == 1
        assert result.failed_repos[0] == "OmniNode-ai/bad_repo"
        assert result.repo_results[0].success is True
        assert result.repo_results[1].success is False

    def test_timeout_produces_error(self) -> None:
        """Subprocess timeout produces a scan error, not an exception."""
        input_model = ModelPrSnapshotInput(repos=("OmniNode-ai/slow_repo",))

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 30)):
            result = HandlerPrSnapshot().handle(input_model)

        assert result.total_prs == 0
        assert len(result.failed_repos) == 1
        assert "Timeout" in (result.repo_results[0].error or "")

    def test_draft_filtering(self) -> None:
        """When include_drafts=False, draft PRs are excluded."""
        prs = [
            _make_gh_pr_json(number=1, is_draft=False),
            _make_gh_pr_json(number=2, is_draft=True),
            _make_gh_pr_json(number=3, is_draft=False),
        ]
        input_model = ModelPrSnapshotInput(
            repos=("OmniNode-ai/test",),
            include_drafts=False,
        )

        with patch("subprocess.run", return_value=_mock_subprocess_run(prs)):
            result = HandlerPrSnapshot().handle(input_model)

        assert result.total_prs == 2
        assert {pr.number for pr in result.all_prs} == {1, 3}

    def test_drafts_included_by_default(self) -> None:
        """By default, draft PRs are included."""
        prs = [
            _make_gh_pr_json(number=1, is_draft=True),
            _make_gh_pr_json(number=2, is_draft=False),
        ]
        input_model = ModelPrSnapshotInput(repos=("OmniNode-ai/test",))

        with patch("subprocess.run", return_value=_mock_subprocess_run(prs)):
            result = HandlerPrSnapshot().handle(input_model)

        assert result.total_prs == 2

    def test_all_prs_feeds_into_merge_sweep_request(self) -> None:
        """all_prs output wires directly into ModelMergeSweepRequest(prs=...)."""
        prs = [
            _make_gh_pr_json(number=1, mergeable="MERGEABLE"),
            _make_gh_pr_json(number=2, mergeable="CONFLICTING"),
        ]
        input_model = ModelPrSnapshotInput(repos=("OmniNode-ai/test",))

        with patch("subprocess.run", return_value=_mock_subprocess_run(prs)):
            result = HandlerPrSnapshot().handle(input_model)

        # Verify direct wiring into ModelMergeSweepRequest
        sweep_request = ModelMergeSweepRequest(prs=result.all_prs)
        assert len(sweep_request.prs) == 2
        assert all(isinstance(pr, ModelPRInfo) for pr in sweep_request.prs)

    def test_handler_type_and_category(self) -> None:
        """Handler reports correct type and category."""
        handler = HandlerPrSnapshot()
        assert handler.handler_type == "NODE_HANDLER"
        assert handler.handler_category == "EFFECT"

    def test_default_repos_contains_all_org_repos(self) -> None:
        """DEFAULT_REPOS includes all expected OmniNode-ai repos."""
        assert len(DEFAULT_REPOS) == 12
        assert all(r.startswith("OmniNode-ai/") for r in DEFAULT_REPOS)
        assert "OmniNode-ai/omniclaude" in DEFAULT_REPOS
        assert "OmniNode-ai/omnimarket" in DEFAULT_REPOS

    def test_empty_repo_list(self) -> None:
        """Empty repos list produces empty result."""
        input_model = ModelPrSnapshotInput(repos=())
        result = HandlerPrSnapshot().handle(input_model)

        assert result.total_prs == 0
        assert len(result.repo_results) == 0
        assert result.all_prs == []
        assert result.failed_repos == []

    def test_json_parse_error_produces_scan_error(self) -> None:
        """Invalid JSON from gh CLI produces a scan error."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not valid json"
        mock_result.stderr = ""

        input_model = ModelPrSnapshotInput(repos=("OmniNode-ai/test",))

        with patch("subprocess.run", return_value=mock_result):
            result = HandlerPrSnapshot().handle(input_model)

        assert len(result.failed_repos) == 1
        assert "JSON parse error" in (result.repo_results[0].error or "")
