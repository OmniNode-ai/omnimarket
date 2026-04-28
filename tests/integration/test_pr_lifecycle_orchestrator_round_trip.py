# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Executable round-trip proof for the downstream PR lifecycle orchestrator."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.runtime.runtime_local_adapter import HandlerBusAdapter

from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    TOPIC_COMPLETED,
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

_START_TOPIC = "onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1"


class _MockInventory:
    def __init__(self, prs: tuple[PrRecord, ...]) -> None:
        self._prs = prs

    def handle(self, input_model: Any) -> InventoryResult:
        return InventoryResult(prs=self._prs, total_collected=len(self._prs))


class _MockTriage:
    def __init__(self, classified: tuple[TriageRecord, ...]) -> None:
        self._classified = classified

    async def handle(self, correlation_id: Any, prs: Any) -> PrTriageResult:
        green = sum(
            1 for record in self._classified if record.category == EnumPrCategory.GREEN
        )
        return PrTriageResult(
            classified=self._classified,
            green_count=green,
            non_green_count=len(self._classified) - green,
        )


class _MockReducer:
    def __init__(self, intents: tuple[ReducerIntent, ...]) -> None:
        self._intents = intents

    async def handle(self, *args: Any, **kwargs: Any) -> ReducerResult:
        merge_count = sum(
            1 for intent in self._intents if intent.intent == EnumReducerIntent.MERGE
        )
        fix_count = sum(
            1 for intent in self._intents if intent.intent == EnumReducerIntent.FIX
        )
        skip_count = sum(
            1 for intent in self._intents if intent.intent == EnumReducerIntent.SKIP
        )
        return ReducerResult(
            intents=self._intents,
            merge_count=merge_count,
            fix_count=fix_count,
            skip_count=skip_count,
        )


class _MockFix:
    async def handle(self, command: Any) -> FixResult:
        return FixResult(prs_dispatched=1, prs_skipped=0)


class _TestOrchestrator(HandlerPrLifecycleOrchestrator):
    def __init__(self, *, _mock_prs: tuple[PrRecord, ...] = (), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._mock_prs = _mock_prs

    def _enumerate_repos(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(pr.repo for pr in self._mock_prs))

    def _enumerate_open_pr_numbers(self, repo: str) -> tuple[int, ...]:
        return tuple(pr.pr_number for pr in self._mock_prs if pr.repo == repo)


class _TypedHandlerWrapper:
    """Bridge HandlerBusAdapter kwargs into the orchestrator's typed command API."""

    def __init__(self, handler: HandlerPrLifecycleOrchestrator) -> None:
        self._handler = handler

    async def handle(self, **payload: Any) -> Any:
        return await self._handler.handle(ModelPrLifecycleStartCommand(**payload))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_event_bus_round_trip_reaches_terminal_topic(
    integration_event_bus: Any,
) -> None:
    """The subscribed command topic must dispatch the orchestrator and publish completion."""
    await integration_event_bus.start()
    try:
        pr = PrRecord(
            pr_number=201,
            repo="OmniNode-ai/omnimarket",
            checks_status="failure",
        )
        triage = TriageRecord(
            pr_number=201,
            repo="OmniNode-ai/omnimarket",
            category=EnumPrCategory.RED,
        )
        orchestrator = _TestOrchestrator(
            _mock_prs=(pr,),
            inventory=_MockInventory((pr,)),
            triage=_MockTriage((triage,)),
            reducer=_MockReducer(
                (
                    ReducerIntent(
                        pr_number=201,
                        repo="OmniNode-ai/omnimarket",
                        intent=EnumReducerIntent.FIX,
                    ),
                )
            ),
            fix=_MockFix(),
        )
        adapter = HandlerBusAdapter(
            handler=_TypedHandlerWrapper(orchestrator),
            handler_name="pr-lifecycle-orchestrator",
            input_model_cls=ModelPrLifecycleStartCommand,
            output_topic=TOPIC_COMPLETED,
            bus=integration_event_bus,
        )

        await integration_event_bus.subscribe(
            _START_TOPIC,
            on_message=adapter.on_message,
            group_id="omnimarket-pr-lifecycle-orchestrator-test",
        )

        command = ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="omn-10182-inmemory",
            fix_only=True,
        )
        await integration_event_bus.publish(
            _START_TOPIC,
            key=None,
            value=command.model_dump_json().encode("utf-8"),
        )

        history = await integration_event_bus.get_event_history(topic=TOPIC_COMPLETED)
        assert len(history) == 1, f"expected 1 terminal event on {TOPIC_COMPLETED}"

        payload = json.loads(history[0].value)
        assert payload["correlation_id"] == str(command.correlation_id)
        assert payload["final_state"] == "COMPLETE"
        assert payload["prs_inventoried"] == 1
        assert payload["prs_fixed"] == 1
        assert payload["prs_merged"] == 0
    finally:
        await integration_event_bus.close()
