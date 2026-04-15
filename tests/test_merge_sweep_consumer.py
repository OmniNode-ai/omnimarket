# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_merge_sweep Kafka consumer wiring.

Tests the topics module constants and the _build_request helper that
translates a Kafka command payload into a ModelMergeSweepRequest.
"""

from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest

from omnimarket.nodes.node_merge_sweep.handlers.handler_merge_sweep import (
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

    def test_defaults_used_when_command_is_empty(self, tmp_path: pathlib.Path) -> None:
        from omnimarket.nodes.node_merge_sweep.consumer import _build_request

        with patch(
            "omnimarket.nodes.node_merge_sweep.consumer._fetch_prs",
            return_value=[],
        ):
            req = _build_request({}, str(tmp_path))

        assert req.require_approval is True
        assert req.merge_method == "squash"
        assert req.max_total_merges == 0
        assert req.skip_polish is False
        assert req.use_lifecycle_ordering is False
        assert req.prs == []

    def test_command_overrides_defaults(self, tmp_path: pathlib.Path) -> None:
        from omnimarket.nodes.node_merge_sweep.consumer import _build_request

        cmd = {
            "repos": "OmniNode-ai/omniclaude",
            "require_approval": False,
            "merge_method": "rebase",
            "max_total_merges": 5,
            "skip_polish": True,
            "use_lifecycle_ordering": True,
            "correlation_id": "test-123",
        }

        with patch(
            "omnimarket.nodes.node_merge_sweep.consumer._fetch_prs",
            return_value=[],
        ):
            req = _build_request(cmd, str(tmp_path))

        assert req.require_approval is False
        assert req.merge_method == "rebase"
        assert req.max_total_merges == 5
        assert req.skip_polish is True
        assert req.use_lifecycle_ordering is True
        assert req.run_id == "test-123"

    def test_repos_list_accepted(self, tmp_path: pathlib.Path) -> None:
        from omnimarket.nodes.node_merge_sweep.consumer import _build_request

        cmd = {"repos": ["OmniNode-ai/omniclaude", "OmniNode-ai/omnidash"]}
        calls: list[str] = []

        def _fake_fetch(repo: str) -> list[dict]:
            calls.append(repo)
            return []

        with patch(
            "omnimarket.nodes.node_merge_sweep.consumer._fetch_prs",
            side_effect=_fake_fetch,
        ):
            _build_request(cmd, str(tmp_path))

        assert calls == ["OmniNode-ai/omniclaude", "OmniNode-ai/omnidash"]
