# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for baseline probe collectors.

Each probe is tested in isolation with all external I/O mocked.
All probes have synchronous collect() methods.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_baseline_capture.handlers.probes.probe_db_row_counts import (
    ProbeDbRowCounts,
)
from omnimarket.nodes.node_baseline_capture.handlers.probes.probe_git_branches import (
    ProbeGitBranches,
)
from omnimarket.nodes.node_baseline_capture.handlers.probes.probe_github_prs import (
    ProbeGitHubPRs,
)
from omnimarket.nodes.node_baseline_capture.handlers.probes.probe_kafka_topics import (
    ProbeKafkaTopics,
)
from omnimarket.nodes.node_baseline_capture.handlers.probes.probe_linear_tickets import (
    ProbeLinearTickets,
)
from omnimarket.nodes.node_baseline_capture.handlers.probes.probe_system_health import (
    ProbeSystemHealth,
)
from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    ModelDbRowCountSnapshot,
    ModelGitBranchSnapshot,
    ModelGitHubPRSnapshot,
    ModelKafkaTopicSnapshot,
    ModelLinearTicketSnapshot,
    ModelServiceHealthSnapshot,
)

# ---------------------------------------------------------------------------
# ProbeGitHubPRs
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProbeGitHubPRs:
    async def test_returns_pr_snapshots(self) -> None:
        """gh pr list success -> returns ModelGitHubPRSnapshot list."""
        pr_data = [
            {
                "number": 42,
                "title": "feat: add probe",
                "repository": {"nameWithOwner": "OmniNode-ai/omnimarket"},
                "state": "OPEN",
                "labels": [{"name": "enhancement"}],
                "createdAt": "2026-04-01T00:00:00Z",
                "statusCheckRollup": None,
            }
        ]
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(pr_data)
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            probe = ProbeGitHubPRs()
            results = await probe.collect()

        assert len(results) > 0
        first = results[0]
        assert isinstance(first, ModelGitHubPRSnapshot)
        assert first.pr_number == 42
        assert first.title == "feat: add probe"
        assert "enhancement" in first.labels

    async def test_returns_empty_on_gh_failure(self) -> None:
        """gh CLI non-zero exit -> returns empty list (non-fatal)."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error: not authenticated"

        with patch("subprocess.run", return_value=mock_result):
            probe = ProbeGitHubPRs()
            results = await probe.collect()

        assert results == []

    async def test_returns_empty_on_exception(self) -> None:
        """subprocess.run raises -> returns empty list (non-fatal)."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 30)):
            probe = ProbeGitHubPRs()
            results = await probe.collect()

        assert results == []

    async def test_ci_status_parsed_from_rollup(self) -> None:
        """statusCheckRollup with FAILURE maps to ci_status='failure'."""
        pr_data = [
            {
                "number": 99,
                "title": "fix: ci",
                "repository": {"nameWithOwner": "OmniNode-ai/omnimarket"},
                "state": "OPEN",
                "labels": [],
                "createdAt": "2026-04-05T12:00:00Z",
                "statusCheckRollup": [{"state": "FAILURE"}],
            }
        ]
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(pr_data)
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            probe = ProbeGitHubPRs()
            results = await probe.collect()

        assert any(r.ci_status == "failure" for r in results)


# ---------------------------------------------------------------------------
# ProbeLinearTickets
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProbeLinearTickets:
    async def test_returns_ticket_snapshots(self) -> None:
        """Valid Linear API response -> returns ModelLinearTicketSnapshot list."""
        api_response = {
            "data": {
                "issues": {
                    "nodes": [
                        {
                            "identifier": "OMN-1234",
                            "title": "Fix the thing",
                            "state": {"name": "In Progress"},
                            "priority": 2,
                            "assignee": {"displayName": "Alice"},
                            "updatedAt": "2026-04-07T10:00:00Z",
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=api_response)

        mock_client = MagicMock()
        mock_client.post = MagicMock(return_value=mock_resp)
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)

        with (
            patch("httpx.Client", return_value=mock_client),
            patch.dict("os.environ", {"LINEAR_API_KEY": "lin_api_test"}),
        ):
            probe = ProbeLinearTickets()
            results = await probe.collect()

        assert len(results) == 1
        assert isinstance(results[0], ModelLinearTicketSnapshot)
        assert results[0].ticket_id == "OMN-1234"
        assert results[0].state == "In Progress"
        assert results[0].assignee == "Alice"

    async def test_returns_empty_when_no_api_key(self) -> None:
        """Missing LINEAR_API_KEY -> returns empty list."""
        with patch.dict("os.environ", {}, clear=True):
            probe = ProbeLinearTickets()
            results = await probe.collect()

        assert results == []

    async def test_returns_empty_on_http_error(self) -> None:
        """HTTP error -> returns empty list (non-fatal)."""
        import httpx

        mock_client = MagicMock()
        mock_client.post = MagicMock(side_effect=httpx.ConnectError("refused"))
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)

        with (
            patch("httpx.Client", return_value=mock_client),
            patch.dict("os.environ", {"LINEAR_API_KEY": "lin_api_test"}),
        ):
            probe = ProbeLinearTickets()
            results = await probe.collect()

        assert results == []


# ---------------------------------------------------------------------------
# ProbeSystemHealth
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProbeSystemHealth:
    async def test_returns_health_snapshots(self) -> None:
        """Healthy HTTP response -> healthy=True, latency populated."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = MagicMock()
        mock_client.get = MagicMock(return_value=mock_resp)
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)

        with (
            patch("httpx.Client", return_value=mock_client),
        ):
            probe = ProbeSystemHealth()
            results = await probe.collect()

        assert len(results) > 0
        assert all(isinstance(r, ModelServiceHealthSnapshot) for r in results)
        assert all(r.healthy is True for r in results)

    async def test_unhealthy_on_http_error(self) -> None:
        """Connection error -> healthy=False, error populated."""
        import httpx

        mock_client = MagicMock()
        mock_client.get = MagicMock(side_effect=httpx.ConnectError("refused"))
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)

        with (
            patch("httpx.Client", return_value=mock_client),
        ):
            probe = ProbeSystemHealth()
            results = await probe.collect()

        assert len(results) > 0
        assert all(r.healthy is False for r in results)
        assert all(r.error is not None for r in results)


# ---------------------------------------------------------------------------
# ProbeKafkaTopics
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProbeKafkaTopics:
    async def test_returns_topic_snapshots_via_kcat(self) -> None:
        """kcat success -> returns ModelKafkaTopicSnapshot list."""
        kcat_output = {
            "topics": [
                {
                    "topic": "onex.cmd.omnimarket.merge-sweep-start.v1",
                    "partitions": [
                        {"id": 0},
                        {"id": 1},
                    ],
                },
                {
                    "topic": "__consumer_offsets",  # should be filtered out
                    "partitions": [{"id": 0}],
                },
            ]
        }
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(kcat_output)
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            probe = ProbeKafkaTopics()
            results = await probe.collect()

        assert len(results) == 1
        assert isinstance(results[0], ModelKafkaTopicSnapshot)
        assert results[0].topic == "onex.cmd.omnimarket.merge-sweep-start.v1"
        assert results[0].partition_count == 2

    async def test_returns_empty_when_kcat_fails(self) -> None:
        """kcat non-zero exit -> returns empty list (non-fatal)."""
        mock_fail = MagicMock()
        mock_fail.returncode = 1
        mock_fail.stdout = ""
        mock_fail.stderr = "command not found"

        with patch("subprocess.run", return_value=mock_fail):
            probe = ProbeKafkaTopics()
            results = await probe.collect()

        assert results == []

    async def test_returns_empty_on_exception(self) -> None:
        """subprocess raises OSError -> returns empty list (non-fatal)."""
        with patch("subprocess.run", side_effect=OSError("kcat not found")):
            probe = ProbeKafkaTopics()
            results = await probe.collect()

        assert results == []


# ---------------------------------------------------------------------------
# ProbeGitBranches
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProbeGitBranches:
    async def test_returns_branch_snapshots_from_tmp(self, tmp_path: Path) -> None:
        """Directly use worktrees_root pointing to tmp_path."""
        worktrees_root = tmp_path / "omni_worktrees"
        ticket_dir = worktrees_root / "OMN-9999"
        repo_dir = ticket_dir / "somerepo"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            result = MagicMock()
            result.returncode = 0
            if "--abbrev-ref" in cmd:
                result.stdout = "jonah/omn-9999-test\n"
            elif "--format=%ct" in cmd:
                result.stdout = "1712534400\n"
            return result

        with (
            patch("subprocess.run", side_effect=fake_run),
            patch(
                "omnimarket.nodes.node_baseline_capture.handlers.probes.probe_git_branches._WORKTREES_ROOT",
                worktrees_root,
            ),
        ):
            probe = ProbeGitBranches()
            results = await probe.collect()

        assert len(results) == 1
        assert isinstance(results[0], ModelGitBranchSnapshot)
        assert results[0].repo == "somerepo"
        assert results[0].branch == "jonah/omn-9999-test"

    async def test_returns_empty_when_worktrees_root_missing(
        self, tmp_path: Path
    ) -> None:
        """Non-existent worktrees root -> returns empty list."""
        missing_root = tmp_path / "nonexistent_worktrees"

        with patch(
            "omnimarket.nodes.node_baseline_capture.handlers.probes.probe_git_branches._WORKTREES_ROOT",
            missing_root,
        ):
            probe = ProbeGitBranches()
            results = await probe.collect()

        assert results == []

    async def test_returns_empty_when_no_git_dirs(self, tmp_path: Path) -> None:
        """Worktrees directory exists but no .git dirs -> returns empty list."""
        worktrees_root = tmp_path / "omni_worktrees"
        ticket_dir = worktrees_root / "OMN-0000"
        repo_dir = ticket_dir / "somerepo"
        repo_dir.mkdir(parents=True)
        # No .git directory created

        with patch(
            "omnimarket.nodes.node_baseline_capture.handlers.probes.probe_git_branches._WORKTREES_ROOT",
            worktrees_root,
        ):
            probe = ProbeGitBranches()
            results = await probe.collect()

        assert results == []


# ---------------------------------------------------------------------------
# ProbeDbRowCounts
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProbeDbRowCounts:
    async def test_returns_empty_when_no_db_url(self) -> None:
        """No DB URL configured -> returns empty list (non-fatal)."""
        with patch.dict("os.environ", {}, clear=True):
            probe = ProbeDbRowCounts()
            results = await probe.collect()

        assert results == []

    async def test_returns_row_counts_via_asyncio_run(self) -> None:
        """asyncpg connect mocked via _fetch_row_counts -> returns snapshots."""
        from omnimarket.nodes.node_baseline_capture.handlers.probes import (
            probe_db_row_counts,
        )

        async def fake_fetch(
            db_url: str, tables: list[str]
        ) -> list[ModelDbRowCountSnapshot]:
            return [ModelDbRowCountSnapshot(table_name=t, row_count=7) for t in tables]

        with (
            patch.object(probe_db_row_counts, "_fetch_row_counts", fake_fetch),
            patch.dict(
                "os.environ",
                {"OMNIBASE_INFRA_DB_URL": "postgresql://postgres:pw@host:5436/db"},
            ),
        ):
            probe = ProbeDbRowCounts()
            results = await probe.collect()

        assert len(results) > 0
        assert all(isinstance(r, ModelDbRowCountSnapshot) for r in results)
        assert all(r.row_count == 7 for r in results)

    async def test_returns_empty_on_connection_failure(self) -> None:
        """_fetch_row_counts raises -> returns empty list (non-fatal)."""
        from omnimarket.nodes.node_baseline_capture.handlers.probes import (
            probe_db_row_counts,
        )

        async def failing_fetch(
            db_url: str, tables: list[str]
        ) -> list[ModelDbRowCountSnapshot]:
            raise ConnectionRefusedError("connection refused")

        with (
            patch.object(probe_db_row_counts, "_fetch_row_counts", failing_fetch),
            patch.dict(
                "os.environ",
                {"OMNIBASE_INFRA_DB_URL": "postgresql://postgres:pw@host:5436/db"},
            ),
        ):
            probe = ProbeDbRowCounts()
            results = await probe.collect()

        assert results == []
