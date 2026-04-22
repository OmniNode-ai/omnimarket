# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-9403: Publisher wiring unit tests.

Verifies that:
1. HandlerPrLifecycleOrchestrator publishes fixer-dispatch-start.v1 for each
   PR entering the FIXING phase.
2. HandlerOverseerVerifierConsumer publishes verification-receipt-start.v1
   when it processes a command with a non-empty task_id.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier_consumer import (
    TOPIC_VERIFICATION_RECEIPT_START,
    HandlerOverseerVerifierConsumer,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    HandlerPrLifecycleOrchestrator,
    ModelPrLifecycleStartCommand,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
    EnumReducerIntent,
    FixResult,
    InventoryResult,
    PrRecord,
    PrTriageResult,
    ReducerIntent,
    ReducerResult,
    TriageRecord,
)

TOPIC_FIXER_DISPATCH_START = "onex.cmd.omnimarket.fixer-dispatch-start.v1"


# ---------------------------------------------------------------------------
# Minimal mocks for orchestrator sub-handlers
# ---------------------------------------------------------------------------


class _MockInventory:
    def __init__(self, prs: tuple[PrRecord, ...]) -> None:
        self._prs = prs

    def handle(self, input_model: Any) -> Any:
        return InventoryResult(prs=self._prs, total_collected=len(self._prs))


class _MockTriage:
    def __init__(self, classified: tuple[TriageRecord, ...]) -> None:
        self._classified = classified

    async def handle(self, correlation_id: Any, prs: Any) -> Any:
        green = sum(1 for r in self._classified if r.category == EnumPrCategory.GREEN)
        return PrTriageResult(
            classified=self._classified,
            green_count=green,
            non_green_count=len(self._classified) - green,
        )


class _MockReducer:
    def __init__(self, intents: tuple[ReducerIntent, ...]) -> None:
        self._intents = intents

    async def handle(self, *args: Any, **kwargs: Any) -> Any:
        fix = sum(1 for i in self._intents if i.intent == EnumReducerIntent.FIX)
        merge = sum(1 for i in self._intents if i.intent == EnumReducerIntent.MERGE)
        return ReducerResult(
            intents=self._intents, fix_count=fix, merge_count=merge, skip_count=0
        )


class _MockFix:
    async def handle(self, command: Any) -> Any:
        return FixResult(prs_dispatched=1, prs_skipped=0)


class _TestOrchestrator(HandlerPrLifecycleOrchestrator):
    def __init__(self, *, _mock_prs: tuple[PrRecord, ...] = (), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._mock_prs = _mock_prs

    def _enumerate_repos(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(pr.repo for pr in self._mock_prs))

    def _enumerate_open_pr_numbers(self, repo: str) -> tuple[int, ...]:
        return tuple(pr.pr_number for pr in self._mock_prs if pr.repo == repo)


# ---------------------------------------------------------------------------
# Test 1: orchestrator publishes fixer-dispatch-start.v1 on FIXING entry
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFixerDispatchStartPublished:
    """fixer-dispatch-start.v1 fires for each PR entering FIXING phase."""

    async def test_publish_fires_for_each_fix_pr(
        self, event_bus: EventBusInmemory
    ) -> None:
        await event_bus.start()

        pr_red_1 = PrRecord(
            pr_number=201, repo="OmniNode-ai/omnimarket", checks_status="failure"
        )
        pr_red_2 = PrRecord(
            pr_number=202, repo="OmniNode-ai/omnimarket", checks_status="failure"
        )
        triage_red_1 = TriageRecord(
            pr_number=201, repo="OmniNode-ai/omnimarket", category=EnumPrCategory.RED
        )
        triage_red_2 = TriageRecord(
            pr_number=202, repo="OmniNode-ai/omnimarket", category=EnumPrCategory.RED
        )

        orch = _TestOrchestrator(
            _mock_prs=(pr_red_1, pr_red_2),
            inventory=_MockInventory((pr_red_1, pr_red_2)),
            triage=_MockTriage((triage_red_1, triage_red_2)),
            reducer=_MockReducer(
                (
                    ReducerIntent(
                        pr_number=201,
                        repo="OmniNode-ai/omnimarket",
                        intent=EnumReducerIntent.FIX,
                    ),
                    ReducerIntent(
                        pr_number=202,
                        repo="OmniNode-ai/omnimarket",
                        intent=EnumReducerIntent.FIX,
                    ),
                )
            ),
            fix=_MockFix(),
            event_bus=event_bus,
        )

        correlation_id = uuid4()
        await orch.handle(
            ModelPrLifecycleStartCommand(
                correlation_id=correlation_id,
                run_id="20260421-000000-test01",
                fix_only=True,
            )
        )

        history = await event_bus.get_event_history(topic=TOPIC_FIXER_DISPATCH_START)
        assert len(history) == 2, (
            f"expected 2 fixer-dispatch-start messages, got {len(history)}"
        )

        pr_numbers = {json.loads(msg.value)["pr_number"] for msg in history}
        assert pr_numbers == {201, 202}

        for msg in history:
            payload = json.loads(msg.value)
            assert payload["correlation_id"] == str(correlation_id)
            assert "stall_category" in payload
            assert "repo" in payload

        await event_bus.close()

    async def test_no_publish_when_no_fix_prs(
        self, event_bus: EventBusInmemory
    ) -> None:
        await event_bus.start()

        pr_green = PrRecord(
            pr_number=301, repo="OmniNode-ai/omnimarket", checks_status="success"
        )
        triage_green = TriageRecord(
            pr_number=301, repo="OmniNode-ai/omnimarket", category=EnumPrCategory.GREEN
        )

        orch = _TestOrchestrator(
            _mock_prs=(pr_green,),
            inventory=_MockInventory((pr_green,)),
            triage=_MockTriage((triage_green,)),
            reducer=_MockReducer(
                (
                    ReducerIntent(
                        pr_number=301,
                        repo="OmniNode-ai/omnimarket",
                        intent=EnumReducerIntent.MERGE,
                    ),
                )
            ),
            fix=_MockFix(),
            event_bus=event_bus,
        )

        await orch.handle(
            ModelPrLifecycleStartCommand(
                correlation_id=uuid4(),
                run_id="20260421-000000-test02",
            )
        )

        history = await event_bus.get_event_history(topic=TOPIC_FIXER_DISPATCH_START)
        assert len(history) == 0

        await event_bus.close()


# ---------------------------------------------------------------------------
# Test 2: overseer verifier consumer publishes verification-receipt-start.v1
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVerificationReceiptStartPublished:
    """verification-receipt-start.v1 fires when consumer has a non-empty task_id."""

    async def test_publish_fires_on_valid_command(
        self, event_bus: EventBusInmemory
    ) -> None:
        await event_bus.start()
        consumer = HandlerOverseerVerifierConsumer(event_bus=event_bus)

        cmd = json.dumps(
            {
                "correlation_id": "corr-omn-9403",
                "task_id": "OMN-9403",
                "status": "completed",
                "domain": "omnimarket",
                "node_id": "node_fixer_dispatcher",
                "schema_version": "1.0",
            }
        ).encode()

        consumer.process(cmd)
        # Let fire-and-forget task execute
        await asyncio.sleep(0)

        history = await event_bus.get_event_history(
            topic=TOPIC_VERIFICATION_RECEIPT_START
        )
        assert len(history) == 1, f"expected 1 receipt-start, got {len(history)}"

        payload = json.loads(history[0].value)
        assert payload["task_id"] == "OMN-9403"
        assert payload["correlation_id"] == "corr-omn-9403"
        assert "claim" in payload

        await event_bus.close()

    def test_no_publish_when_event_bus_absent(self) -> None:
        consumer = HandlerOverseerVerifierConsumer(event_bus=None)

        cmd = json.dumps(
            {
                "correlation_id": "corr-no-bus",
                "task_id": "OMN-9403",
                "status": "completed",
                "domain": "omnimarket",
                "node_id": "node_fixer_dispatcher",
                "schema_version": "1.0",
            }
        ).encode()

        result = consumer.process(cmd)
        data = json.loads(result)
        assert data["passed"] is True
