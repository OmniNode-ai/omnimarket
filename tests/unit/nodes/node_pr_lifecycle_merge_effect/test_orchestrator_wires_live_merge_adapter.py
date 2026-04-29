# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression tests for Track A live merge wiring."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_lifecycle_merge_effect.handlers.adapter_github_merge_queue import (
    GitHubMergeQueueAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_merge_effect.handlers.handler_pr_lifecycle_merge import (
    HandlerPrLifecycleMerge,
    _NoopGitHubMergeAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    HandlerPrLifecycleOrchestrator,
    TriageRecord,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
)


@pytest.mark.unit
class TestOrchestratorWiresLiveMergeAdapter:
    def test_default_merge_handler_has_live_adapter_not_noop(self) -> None:
        orch = HandlerPrLifecycleOrchestrator()
        orch._ensure_sub_handlers()

        merge = orch._merge
        assert isinstance(merge, HandlerPrLifecycleMerge)
        assert isinstance(merge._github, GitHubMergeQueueAdapter), (
            "Track A green-PR merge must use the live merge-queue adapter; "
            "the no-op adapter is dry-run/test-only."
        )
        assert not isinstance(merge._github, _NoopGitHubMergeAdapter)

    async def test_merge_fanout_requests_merge_queue(self) -> None:
        class _RecordingMerge:
            def __init__(self) -> None:
                self.commands: list[Any] = []

            async def handle(self, command: object) -> object:
                self.commands.append(command)

                class _Result:
                    merged = True

                return _Result()

        recorder = _RecordingMerge()
        orch = HandlerPrLifecycleOrchestrator(merge=recorder)

        result = await orch._call_merge_fanout(
            correlation_id=uuid4(),
            prs_to_merge=(
                TriageRecord(
                    pr_number=42,
                    repo="OmniNode-ai/omnimarket",
                    category=EnumPrCategory.GREEN,
                    block_reason="",
                ),
            ),
            dry_run=False,
        )

        assert result.prs_merged == 1
        assert len(recorder.commands) == 1
        command = recorder.commands[0]
        assert command.use_merge_queue is True
