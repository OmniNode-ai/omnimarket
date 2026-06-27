# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Executable round-trip proof for the downstream PR lifecycle orchestrator."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

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
from omnimarket.nodes.node_pr_lifecycle_orchestrator.verify_target_mapping import (
    EnumVerificationOutcome,
    EnumVerificationTarget,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

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
    """Bridge local runtime adapter kwargs into the orchestrator's typed command API."""

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
            event_bus=integration_event_bus,
        )
        adapter = LocalRuntimeBusAdapter(
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


# ===========================================================================
# WS-5 Wave 3 — multi-parameter coverage (OMN-13677)
# ===========================================================================
#
# Extends the single-case round-trip above into a parametrized Variant B suite
# over the real entry-flag modes (fix-only / inventory-only / merge-only /
# mixed green+red) AND the verify=True VERIFYING path (OMN-13673), asserting the
# distinct terminal-event payload counts each mode produces. The gh/git boundary
# is mocked through the orchestrator's overridable enumeration + verification
# hooks and the injected sub-handler collaborators — never subprocess.


class _MergeOutcome:
    """Minimal per-PR merge result — the orchestrator reads ``.merged``."""

    def __init__(self, merged: bool) -> None:
        self.merged = merged


class _MockMerge:
    """Merge sub-handler that reports every PR as merged (or not)."""

    def __init__(self, merged: bool = True) -> None:
        self._merged = merged

    async def handle(self, command: Any) -> _MergeOutcome:
        return _MergeOutcome(self._merged)


class _ParamOrchestrator(_TestOrchestrator):
    """Orchestrator with deterministic verification hooks for the verify path.

    ``_verification_target_for`` returns a real (non-skip) target so the
    VERIFYING phase runs the probe; ``_execute_verification_probe`` returns the
    injected outcome so no live Docker/node is required.
    """

    def __init__(
        self,
        *,
        verification_outcome: EnumVerificationOutcome | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._verification_outcome = verification_outcome

    def _verification_target_for(self, repo: str, pr_number: int):  # type: ignore[no-untyped-def]
        return EnumVerificationTarget.RUNTIME_HEALTH

    async def _execute_verification_probe(  # type: ignore[override]
        self, *, target: Any, timeout_seconds: int
    ) -> EnumVerificationOutcome:
        assert self._verification_outcome is not None
        return self._verification_outcome


def _green_pr(pr_number: int, repo: str = "OmniNode-ai/omnimarket") -> PrRecord:
    return PrRecord(pr_number=pr_number, repo=repo, checks_status="success")


def _red_pr(pr_number: int, repo: str = "OmniNode-ai/omnimarket") -> PrRecord:
    return PrRecord(pr_number=pr_number, repo=repo, checks_status="failure")


def _triage(
    pr_number: int, category: EnumPrCategory, repo: str = "OmniNode-ai/omnimarket"
) -> TriageRecord:
    return TriageRecord(pr_number=pr_number, repo=repo, category=category)


def _intent(
    pr_number: int, intent: EnumReducerIntent, repo: str = "OmniNode-ai/omnimarket"
) -> ReducerIntent:
    return ReducerIntent(pr_number=pr_number, repo=repo, intent=intent)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "command_kwargs", "expect"),
    [
        # fix-only: a RED PR is routed to FIX, never merged.
        pytest.param(
            {
                "prs": (_red_pr(301),),
                "triage": (_triage(301, EnumPrCategory.RED),),
                "intents": (_intent(301, EnumReducerIntent.FIX),),
                "merge_ok": True,
                "verify_outcome": None,
            },
            {"fix_only": True},
            {
                "final_state": "COMPLETE",
                "prs_inventoried": 1,
                "prs_fixed": 1,
                "prs_merged": 0,
                "prs_verified": 0,
                "prs_verification_blocked": 0,
            },
            id="fix-only",
        ),
        # inventory-only: stop after inventory; nothing merged or fixed.
        pytest.param(
            {
                "prs": (_green_pr(302),),
                "triage": (_triage(302, EnumPrCategory.GREEN),),
                "intents": (_intent(302, EnumReducerIntent.MERGE),),
                "merge_ok": True,
                "verify_outcome": None,
            },
            {"inventory_only": True},
            {
                "final_state": "COMPLETE",
                "prs_inventoried": 1,
                "prs_fixed": 0,
                "prs_merged": 0,
                "prs_verified": 0,
                "prs_verification_blocked": 0,
            },
            id="inventory-only",
        ),
        # merge-only: a GREEN PR is merged; fix phase skipped.
        pytest.param(
            {
                "prs": (_green_pr(303),),
                "triage": (_triage(303, EnumPrCategory.GREEN),),
                "intents": (_intent(303, EnumReducerIntent.MERGE),),
                "merge_ok": True,
                "verify_outcome": None,
            },
            {"merge_only": True},
            {
                "final_state": "COMPLETE",
                "prs_inventoried": 1,
                "prs_fixed": 0,
                "prs_merged": 1,
                "prs_verified": 0,
                "prs_verification_blocked": 0,
            },
            id="merge-only",
        ),
        # mixed: one GREEN merges, one RED is routed to FIX (negative control —
        # the red PR is NOT merged).
        pytest.param(
            {
                "prs": (_green_pr(304), _red_pr(305)),
                "triage": (
                    _triage(304, EnumPrCategory.GREEN),
                    _triage(305, EnumPrCategory.RED),
                ),
                "intents": (
                    _intent(304, EnumReducerIntent.MERGE),
                    _intent(305, EnumReducerIntent.FIX),
                ),
                "merge_ok": True,
                "verify_outcome": None,
            },
            {},
            {
                "final_state": "COMPLETE",
                "prs_inventoried": 2,
                "prs_fixed": 1,
                "prs_merged": 1,
                "prs_verified": 0,
                "prs_verification_blocked": 0,
            },
            id="mixed-green-red",
        ),
        # verify=True, probe passes (MERGED): PR is verified then merged.
        pytest.param(
            {
                "prs": (_green_pr(306),),
                "triage": (_triage(306, EnumPrCategory.GREEN),),
                "intents": (_intent(306, EnumReducerIntent.MERGE),),
                "merge_ok": True,
                "verify_outcome": EnumVerificationOutcome.MERGED,
            },
            {"verify": True},
            {
                "final_state": "COMPLETE",
                "prs_inventoried": 1,
                "prs_fixed": 0,
                "prs_merged": 1,
                "prs_verified": 1,
                "prs_verification_blocked": 0,
            },
            id="verify-pass",
        ),
        # verify=True, probe fails (VERIFICATION_FAILED): PR blocked, NOT merged
        # (negative control on the verify gate, OMN-13673).
        pytest.param(
            {
                "prs": (_green_pr(307),),
                "triage": (_triage(307, EnumPrCategory.GREEN),),
                "intents": (_intent(307, EnumReducerIntent.MERGE),),
                "merge_ok": True,
                "verify_outcome": EnumVerificationOutcome.VERIFICATION_FAILED,
            },
            {"verify": True},
            {
                "final_state": "COMPLETE",
                "prs_inventoried": 1,
                "prs_fixed": 0,
                "prs_merged": 0,
                "prs_verified": 0,
                "prs_verification_blocked": 1,
            },
            id="verify-fail-blocks-merge",
        ),
    ],
)
async def test_pr_lifecycle_round_trip_multiparam(
    integration_event_bus: Any,
    tmp_path: Any,
    monkeypatch: Any,
    spec: dict[str, Any],
    command_kwargs: dict[str, Any],
    expect: dict[str, int | str],
) -> None:
    """Bus round-trip across every entry-flag mode + the verify=True path."""
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    await integration_event_bus.start()
    try:
        prs = spec["prs"]
        triage = spec["triage"]
        intents = spec["intents"]
        orchestrator = _ParamOrchestrator(
            _mock_prs=prs,
            inventory=_MockInventory(prs),
            triage=_MockTriage(triage),
            reducer=_MockReducer(intents),
            merge=_MockMerge(spec["merge_ok"]),
            fix=_MockFix(),
            event_bus=integration_event_bus,
            verification_outcome=spec["verify_outcome"],
        )
        adapter = LocalRuntimeBusAdapter(
            handler=_TypedHandlerWrapper(orchestrator),
            handler_name="pr-lifecycle-orchestrator",
            input_model_cls=ModelPrLifecycleStartCommand,
            output_topic=TOPIC_COMPLETED,
            bus=integration_event_bus,
        )
        await integration_event_bus.subscribe(
            _START_TOPIC,
            on_message=adapter.on_message,
            group_id="omn-13677-pr-lifecycle-multiparam",
        )

        command = ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="omn-13677-multiparam",
            **command_kwargs,
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
        for key, value in expect.items():
            assert payload[key] == value, f"{key}: expected {value}, got {payload[key]}"

        # The 7-category verification breakdown is always materialized; on the
        # verify=True cases the injected outcome must appear with count 1.
        verify_outcome = spec["verify_outcome"]
        if verify_outcome is not None:
            breakdown = payload["verification_breakdown"]
            assert breakdown[verify_outcome.value] == 1, breakdown
    finally:
        await integration_event_bus.close()
