# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_merge_sweep_compute Kafka consumer wiring.

Tests the topics module constants and the _build_request helper that
translates a Kafka command payload into a ModelMergeSweepRequest.
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    TOPIC_MERGE_SWEEP_COMPLETED,
    TOPIC_MERGE_SWEEP_START,
)


@pytest.mark.unit
class TestMergeSweepTopics:
    """Topics module declares exactly the strings from contract.yaml."""

    def test_start_topic_matches_contract(self) -> None:
        assert TOPIC_MERGE_SWEEP_START == "onex.cmd.omnimarket.merge-sweep-start.v1"

    def test_completed_topic_matches_contract(self) -> None:
        assert (
            TOPIC_MERGE_SWEEP_COMPLETED
            == "onex.evt.omnimarket.merge-sweep-completed.v1"
        )


@pytest.mark.unit
class TestBuildRequest:
    """_build_request translates Kafka command payload → ModelMergeSweepRequest."""

    def _make_stub_client(self, prs: list[dict] | None = None) -> MagicMock:
        client = MagicMock()
        client.fetch_open_prs.return_value = prs or []
        client.fetch_branch_protection.return_value = None
        return client

    def test_defaults_used_when_command_is_empty(self, tmp_path: pathlib.Path) -> None:
        from omnimarket.nodes.node_merge_sweep_compute.consumer import _build_request

        github = self._make_stub_client()
        req = _build_request(github, {}, str(tmp_path))

        assert req.require_approval is True
        assert req.merge_method == "squash"
        assert req.max_total_merges == 0
        assert req.skip_polish is False
        assert req.use_lifecycle_ordering is False
        assert req.prs == []

    def test_command_overrides_defaults(self, tmp_path: pathlib.Path) -> None:
        from omnimarket.nodes.node_merge_sweep_compute.consumer import _build_request

        cmd = {
            "repos": "OmniNode-ai/omniclaude",
            "require_approval": False,
            "merge_method": "rebase",
            "max_total_merges": 5,
            "skip_polish": True,
            "use_lifecycle_ordering": True,
            "correlation_id": "test-123",
        }

        github = self._make_stub_client()
        req = _build_request(github, cmd, str(tmp_path))

        assert req.require_approval is False
        assert req.merge_method == "rebase"
        assert req.max_total_merges == 5
        assert req.skip_polish is True
        assert req.use_lifecycle_ordering is True
        assert req.run_id == "test-123"

    def test_repos_list_accepted(self, tmp_path: pathlib.Path) -> None:
        from omnimarket.nodes.node_merge_sweep_compute.consumer import _build_request

        cmd = {"repos": ["OmniNode-ai/omniclaude", "OmniNode-ai/omnidash"]}
        github = self._make_stub_client()
        _build_request(github, cmd, str(tmp_path))

        assert github.fetch_open_prs.call_count == 2

    def test_transport_error_skips_repo(self, tmp_path: pathlib.Path) -> None:
        from omnimarket.nodes.node_merge_sweep_compute.consumer import _build_request
        from omnimarket.nodes.node_merge_sweep_compute.protocols import (
            GitHubTransportError,
        )

        github = MagicMock()
        github.fetch_branch_protection.return_value = None
        github.fetch_open_prs.side_effect = GitHubTransportError("network down")

        cmd = {"repos": "OmniNode-ai/omniclaude"}
        req = _build_request(github, cmd, str(tmp_path))

        assert req.prs == []

    def test_invalid_repo_format_skips_repo(self, tmp_path: pathlib.Path) -> None:
        from omnimarket.nodes.node_merge_sweep_compute.consumer import _build_request

        github = MagicMock()
        github.fetch_branch_protection.return_value = None
        github.fetch_open_prs.side_effect = ValueError("Invalid repo format")

        cmd = {"repos": "not-a-valid-repo-format"}
        req = _build_request(github, cmd, str(tmp_path))

        assert req.prs == []
