# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_pr_lifecycle_merge_effect.

Verifies the merge effect node: green PR merge execution, merge queue policy
routing, non-green verdict rejection, adapter exception handling, dry-run
path, ProtocolGitHubMergeAdapter injection, and EventBusInmemory wiring.

Related:
    - OMN-8084: Create pr_lifecycle_merge_effect Node
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_pr_lifecycle_merge_effect.handlers.handler_pr_lifecycle_merge import (
    HandlerPrLifecycleMerge,
    ProtocolGitHubMergeAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_merge_effect.models.model_merge_command import (
    ModelPrMergeCommand,
)
from omnimarket.nodes.node_pr_lifecycle_merge_effect.models.model_merge_result import (
    ModelPrMergeResult,
)

# ---------------------------------------------------------------------------
# Test adapters
# ---------------------------------------------------------------------------


class _RecordingGitHubMergeAdapter:
    """Records merge calls for assertion."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def merge_pr(
        self,
        repo: str,
        pr_number: int,
        use_merge_queue: bool,
    ) -> str:
        self.calls.append(
            {"repo": repo, "pr_number": pr_number, "use_merge_queue": use_merge_queue}
        )
        strategy = "queue" if use_merge_queue else "squash"
        return f"auto-merge enabled ({strategy}) for {repo}#{pr_number}"


class _FailingGitHubMergeAdapter:
    """Always raises on merge_pr."""

    async def merge_pr(
        self,
        repo: str,
        pr_number: int,
        use_merge_queue: bool,
    ) -> str:
        msg = "GitHub API error: PR not mergeable"
        raise RuntimeError(msg)


def _make_command(
    pr_number: int = 42,
    repo: str = "OmniNode-ai/omnimarket",
    triage_verdict: str = "green",
    use_merge_queue: bool = False,
    ticket_id: str | None = "OMN-8084",
    dry_run: bool = False,
    correlation_id: UUID | None = None,
) -> ModelPrMergeCommand:
    return ModelPrMergeCommand(
        correlation_id=correlation_id or uuid4(),
        pr_number=pr_number,
        repo=repo,
        triage_verdict=triage_verdict,
        use_merge_queue=use_merge_queue,
        ticket_id=ticket_id,
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


# ---------------------------------------------------------------------------
# Golden chain tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPrLifecycleMergeEffectGoldenChain:
    """Golden chain: merge command -> adapter call -> result."""

    async def test_green_pr_squash_merge(self) -> None:
        """Green PR with use_merge_queue=False triggers squash auto-merge."""
        adapter = _RecordingGitHubMergeAdapter()
        handler = HandlerPrLifecycleMerge(github_adapter=adapter)
        cid = uuid4()
        command = _make_command(
            pr_number=101,
            repo="OmniNode-ai/omnimarket",
            triage_verdict="green",
            use_merge_queue=False,
            correlation_id=cid,
        )

        result = await handler.handle(command)

        assert isinstance(result, ModelPrMergeResult)
        assert result.correlation_id == cid
        assert result.pr_number == 101
        assert result.repo == "OmniNode-ai/omnimarket"
        assert result.merged is True
        assert result.error is None
        assert "squash" in result.merge_action

        assert len(adapter.calls) == 1
        assert adapter.calls[0]["pr_number"] == 101
        assert adapter.calls[0]["use_merge_queue"] is False

    async def test_green_pr_merge_queue(self) -> None:
        """Green PR with use_merge_queue=True uses queue path."""
        adapter = _RecordingGitHubMergeAdapter()
        handler = HandlerPrLifecycleMerge(github_adapter=adapter)
        command = _make_command(
            pr_number=202,
            repo="OmniNode-ai/omniclaude",
            triage_verdict="green",
            use_merge_queue=True,
        )

        result = await handler.handle(command)

        assert result.merged is True
        assert result.error is None
        assert "queue" in result.merge_action
        assert adapter.calls[0]["use_merge_queue"] is True

    async def test_non_green_verdict_rejected(self) -> None:
        """Non-green triage verdict is rejected without calling adapter."""
        adapter = _RecordingGitHubMergeAdapter()
        handler = HandlerPrLifecycleMerge(github_adapter=adapter)

        for verdict in ("red", "conflicted", "needs_review"):
            command = _make_command(triage_verdict=verdict)
            result = await handler.handle(command)

            assert result.merged is False
            assert result.error is not None
            assert verdict in result.merge_action

        assert len(adapter.calls) == 0

    async def test_adapter_exception_produces_error_result(self) -> None:
        """If the adapter raises, merged=False and error is set."""
        handler = HandlerPrLifecycleMerge(github_adapter=_FailingGitHubMergeAdapter())
        command = _make_command(triage_verdict="green")

        result = await handler.handle(command)

        assert result.merged is False
        assert result.error is not None
        assert "GitHub API error" in result.error

    async def test_dry_run_uses_noop_adapter(self) -> None:
        """Default handler (no adapter injected) acts as noop — dry_run safe."""
        handler = HandlerPrLifecycleMerge()
        command = _make_command(triage_verdict="green", dry_run=True)

        result = await handler.handle(command)

        assert result.merged is True
        assert "[noop]" in result.merge_action
        assert result.error is None

    async def test_dry_run_with_injected_adapter_does_not_call_adapter(self) -> None:
        """dry_run=True should bypass injected adapter side effects."""
        adapter = _RecordingGitHubMergeAdapter()
        handler = HandlerPrLifecycleMerge(github_adapter=adapter)
        command = _make_command(triage_verdict="green", dry_run=True)

        result = await handler.handle(command)

        assert result.merged is True
        assert "[noop]" in result.merge_action
        assert result.error is None
        assert len(adapter.calls) == 0

    async def test_completed_at_is_set(self) -> None:
        """completed_at is set on both success and failure paths."""
        adapter = _RecordingGitHubMergeAdapter()
        handler = HandlerPrLifecycleMerge(github_adapter=adapter)
        command = _make_command(triage_verdict="green")

        result = await handler.handle(command)

        assert isinstance(result.completed_at, datetime)
        assert result.completed_at.tzinfo is not None

    async def test_handler_type_and_category(self) -> None:
        """Handler reports correct type and category."""
        handler = HandlerPrLifecycleMerge()
        assert handler.handler_type == "NODE_HANDLER"
        assert handler.handler_category == "EFFECT"

    async def test_correlation_id_propagated(self) -> None:
        """correlation_id from command is propagated to result."""
        adapter = _RecordingGitHubMergeAdapter()
        handler = HandlerPrLifecycleMerge(github_adapter=adapter)
        cid = uuid4()
        command = _make_command(triage_verdict="green", correlation_id=cid)

        result = await handler.handle(command)

        assert result.correlation_id == cid

    async def test_protocol_is_runtime_checkable(self) -> None:
        """ProtocolGitHubMergeAdapter is runtime_checkable."""
        assert isinstance(_RecordingGitHubMergeAdapter(), ProtocolGitHubMergeAdapter)
        assert isinstance(_FailingGitHubMergeAdapter(), ProtocolGitHubMergeAdapter)

    async def test_ticket_id_optional(self) -> None:
        """ticket_id=None is allowed and does not affect merge execution."""
        adapter = _RecordingGitHubMergeAdapter()
        handler = HandlerPrLifecycleMerge(github_adapter=adapter)
        command = _make_command(triage_verdict="green", ticket_id=None)

        result = await handler.handle(command)

        assert result.merged is True


@pytest.mark.unit
class TestPrLifecycleMergeEffectEventBus:
    """Event bus wiring: triage-completed event -> merge-completed event."""

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired: triage-completed in -> merge-completed out."""
        adapter = _RecordingGitHubMergeAdapter()
        handler = HandlerPrLifecycleMerge(github_adapter=adapter)
        merge_events: list[dict[str, object]] = []

        async def on_triage_completed(message: object) -> None:
            payload: dict[str, object] = {}
            if isinstance(message, bytes | bytearray):
                payload = json.loads(message.decode())
            elif isinstance(message, str):
                payload = json.loads(message)
            elif isinstance(message, dict):
                payload = message
            command = _make_command(
                pr_number=int(payload.get("pr_number", 777)),
                repo=str(payload.get("repo", "OmniNode-ai/omnimarket")),
                triage_verdict=str(payload.get("category", "green")),
                use_merge_queue=bool(payload.get("use_merge_queue", False)),
                correlation_id=UUID(payload["correlation_id"])
                if "correlation_id" in payload
                else uuid4(),
            )
            result = await handler.handle(command)
            event: dict[str, object] = {
                "correlation_id": str(result.correlation_id),
                "pr_number": result.pr_number,
                "repo": result.repo,
                "merged": result.merged,
                "merge_action": result.merge_action,
            }
            merge_events.append(event)
            await event_bus.publish(
                "onex.evt.omnimarket.pr-lifecycle-merge-completed.v1",
                key=None,
                value=json.dumps(event).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            "onex.evt.omnimarket.pr-lifecycle-triage-completed.v1",
            on_message=on_triage_completed,
            group_id="test-merge",
        )

        await event_bus.publish(
            "onex.evt.omnimarket.pr-lifecycle-triage-completed.v1",
            key=None,
            value=b'{"category": "green", "pr_number": 777}',
        )

        assert len(merge_events) == 1
        assert merge_events[0]["merged"] is True
        assert merge_events[0]["pr_number"] == 777

        history = await event_bus.get_event_history(
            topic="onex.evt.omnimarket.pr-lifecycle-merge-completed.v1"
        )
        assert len(history) == 1

        await event_bus.close()
