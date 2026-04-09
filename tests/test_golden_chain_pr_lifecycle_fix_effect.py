"""Golden chain tests for node_pr_lifecycle_fix_effect.

Verifies fix routing by block reason using mock adapters (zero infrastructure),
dry_run mode, error isolation, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)

CMD_TOPIC = "onex.cmd.omnimarket.pr-lifecycle-fix-start.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.pr-lifecycle-fix-completed.v1"


# ---------------------------------------------------------------------------
# Mock adapters
# ---------------------------------------------------------------------------


class _MockGitHubAdapter:
    def __init__(self) -> None:
        self.rerun_calls: list[tuple[str, int]] = []
        self.conflict_calls: list[tuple[str, int]] = []

    async def rerun_failed_checks(self, repo: str, pr_number: int) -> str:
        self.rerun_calls.append((repo, pr_number))
        return f"[mock] rerequested CI on {repo}#{pr_number}"

    async def resolve_conflicts(self, repo: str, pr_number: int) -> str:
        self.conflict_calls.append((repo, pr_number))
        return f"[mock] resolved conflicts on {repo}#{pr_number}"


class _MockAgentDispatchAdapter:
    def __init__(self) -> None:
        self.review_fix_calls: list[tuple[str, int, str | None]] = []
        self.coderabbit_calls: list[tuple[str, int]] = []

    async def dispatch_review_fix(
        self, repo: str, pr_number: int, ticket_id: str | None
    ) -> str:
        self.review_fix_calls.append((repo, pr_number, ticket_id))
        return f"[mock] dispatched review-fix on {repo}#{pr_number}"

    async def dispatch_coderabbit_reply(self, repo: str, pr_number: int) -> str:
        self.coderabbit_calls.append((repo, pr_number))
        return f"[mock] dispatched coderabbit-reply on {repo}#{pr_number}"


class _FailingGitHubAdapter:
    async def rerun_failed_checks(self, repo: str, pr_number: int) -> str:
        raise RuntimeError("CI service unavailable")

    async def resolve_conflicts(self, repo: str, pr_number: int) -> str:
        raise RuntimeError("conflict resolver unavailable")


def _make_command(
    block_reason: EnumPrBlockReason = EnumPrBlockReason.CI_FAILURE,
    pr_number: int = 42,
    repo: str = "OmniNode-ai/omnimarket",
    ticket_id: str | None = "OMN-8085",
    dry_run: bool = False,
) -> ModelPrLifecycleFixCommand:
    return ModelPrLifecycleFixCommand(
        correlation_id=uuid4(),
        pr_number=pr_number,
        repo=repo,
        block_reason=block_reason,
        ticket_id=ticket_id,
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestPrLifecycleFixEffectGoldenChain:
    """Golden chain: fix command -> adapter routing -> result."""

    async def test_ci_failure_routes_to_github_rerun(
        self, event_bus: EventBusInmemory
    ) -> None:
        """ci_failure block reason -> GitHub rerun_failed_checks."""
        gh = _MockGitHubAdapter()
        agent = _MockAgentDispatchAdapter()
        handler = HandlerPrLifecycleFix(github_adapter=gh, agent_dispatch_adapter=agent)
        command = _make_command(block_reason=EnumPrBlockReason.CI_FAILURE)

        result = await handler.handle(command)

        assert result.fix_applied is True
        assert result.block_reason == EnumPrBlockReason.CI_FAILURE
        assert "rerequested CI" in result.fix_action
        assert result.error is None
        assert gh.rerun_calls == [("OmniNode-ai/omnimarket", 42)]
        assert agent.review_fix_calls == []

    async def test_conflict_routes_to_github_resolve(
        self, event_bus: EventBusInmemory
    ) -> None:
        """conflict block reason -> GitHub resolve_conflicts."""
        gh = _MockGitHubAdapter()
        agent = _MockAgentDispatchAdapter()
        handler = HandlerPrLifecycleFix(github_adapter=gh, agent_dispatch_adapter=agent)
        command = _make_command(block_reason=EnumPrBlockReason.CONFLICT)

        result = await handler.handle(command)

        assert result.fix_applied is True
        assert result.block_reason == EnumPrBlockReason.CONFLICT
        assert "resolved conflicts" in result.fix_action
        assert gh.conflict_calls == [("OmniNode-ai/omnimarket", 42)]

    async def test_changes_requested_routes_to_review_fix(
        self, event_bus: EventBusInmemory
    ) -> None:
        """changes_requested block reason -> agent dispatch_review_fix."""
        gh = _MockGitHubAdapter()
        agent = _MockAgentDispatchAdapter()
        handler = HandlerPrLifecycleFix(github_adapter=gh, agent_dispatch_adapter=agent)
        command = _make_command(block_reason=EnumPrBlockReason.CHANGES_REQUESTED)

        result = await handler.handle(command)

        assert result.fix_applied is True
        assert result.block_reason == EnumPrBlockReason.CHANGES_REQUESTED
        assert "review-fix" in result.fix_action
        assert agent.review_fix_calls == [("OmniNode-ai/omnimarket", 42, "OMN-8085")]

    async def test_coderabbit_routes_to_coderabbit_reply(
        self, event_bus: EventBusInmemory
    ) -> None:
        """coderabbit block reason -> agent dispatch_coderabbit_reply."""
        gh = _MockGitHubAdapter()
        agent = _MockAgentDispatchAdapter()
        handler = HandlerPrLifecycleFix(github_adapter=gh, agent_dispatch_adapter=agent)
        command = _make_command(block_reason=EnumPrBlockReason.CODERABBIT)

        result = await handler.handle(command)

        assert result.fix_applied is True
        assert result.block_reason == EnumPrBlockReason.CODERABBIT
        assert "coderabbit-reply" in result.fix_action
        assert agent.coderabbit_calls == [("OmniNode-ai/omnimarket", 42)]

    async def test_dry_run_uses_noop_adapters(
        self, event_bus: EventBusInmemory
    ) -> None:
        """dry_run=True: noop adapters produce [noop] action strings, no real I/O."""
        handler = HandlerPrLifecycleFix()  # no adapters injected -> noop
        command = _make_command(block_reason=EnumPrBlockReason.CI_FAILURE, dry_run=True)

        result = await handler.handle(command)

        assert result.fix_applied is True
        assert "[noop]" in result.fix_action
        assert result.error is None

    async def test_adapter_error_captured_in_result(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Adapter exception -> fix_applied=False, error captured, no raise."""
        handler = HandlerPrLifecycleFix(github_adapter=_FailingGitHubAdapter())
        command = _make_command(block_reason=EnumPrBlockReason.CI_FAILURE)

        result = await handler.handle(command)

        assert result.fix_applied is False
        assert result.error is not None
        assert "unavailable" in result.error

    async def test_result_fields_populated(self, event_bus: EventBusInmemory) -> None:
        """Result has all required fields set."""
        handler = HandlerPrLifecycleFix(
            github_adapter=_MockGitHubAdapter(),
            agent_dispatch_adapter=_MockAgentDispatchAdapter(),
        )
        command = _make_command(
            block_reason=EnumPrBlockReason.CONFLICT,
            pr_number=99,
            repo="OmniNode-ai/test-repo",
        )

        result = await handler.handle(command)

        assert result.correlation_id == command.correlation_id
        assert result.pr_number == 99
        assert result.repo == "OmniNode-ai/test-repo"
        assert result.completed_at is not None

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler result can be published and consumed via EventBusInmemory."""
        handler = HandlerPrLifecycleFix(
            github_adapter=_MockGitHubAdapter(),
            agent_dispatch_adapter=_MockAgentDispatchAdapter(),
        )
        completed_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
                ModelPrLifecycleFixCommand,
            )

            cmd = ModelPrLifecycleFixCommand(
                correlation_id=payload["correlation_id"],
                pr_number=payload["pr_number"],
                repo=payload["repo"],
                block_reason=payload["block_reason"],
                ticket_id=payload.get("ticket_id"),
                requested_at=datetime.now(tz=UTC),
            )
            result = await handler.handle(cmd)
            result_payload = result.model_dump(mode="json")
            completed_events.append(result_payload)
            await event_bus.publish(
                COMPLETED_TOPIC,
                key=None,
                value=json.dumps(result_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-pr-lifecycle-fix"
        )

        cmd_payload = json.dumps(
            {
                "correlation_id": str(uuid4()),
                "pr_number": 42,
                "repo": "OmniNode-ai/omnimarket",
                "block_reason": "ci_failure",
                "ticket_id": "OMN-8085",
            }
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["fix_applied"] is True
        assert completed_events[0]["block_reason"] == "ci_failure"

        await event_bus.close()

    async def test_all_block_reasons_covered(self, event_bus: EventBusInmemory) -> None:
        """All four EnumPrBlockReason values route without error."""
        gh = _MockGitHubAdapter()
        agent = _MockAgentDispatchAdapter()
        handler = HandlerPrLifecycleFix(github_adapter=gh, agent_dispatch_adapter=agent)

        for reason in EnumPrBlockReason:
            command = _make_command(block_reason=reason)
            result = await handler.handle(command)
            assert result.fix_applied is True, (
                f"reason={reason} should route successfully"
            )
            assert result.error is None, f"reason={reason} should not error"
