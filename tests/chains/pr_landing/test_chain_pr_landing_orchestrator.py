# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and agent chains through the PR landing orchestrator (OMN-19829).

Each case drives the real orchestrator handler with the real reducer (T6) and
the real row store under compare-and-set, answers every GitHub effect it
requests the way the effect would, and publishes each event the orchestrator
emits on its own topic of an in-memory bus. The chain asserted is what the
landing projection (T11) consumes: one transitioned event per transition in
``seq`` order, then the terminal or the agent-needed event.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import BaseModel

from omnimarket.events.topics import (
    OCC_AUTOBIND_COMMAND_TOPIC_V1,
    PR_LANDING_GITHUB_REQUESTED_TOPIC_V1,
)
from omnimarket.nodes.node_pr_arm_gate_compute.handlers.handler_arm_gate import (
    HandlerPrArmGate,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
    ModelPrLandingGithubRequest,
)
from omnimarket.nodes.node_pr_landing_orchestrator.event_topics import (
    PR_LANDING_EVENT_TOPICS,
)
from omnimarket.nodes.node_pr_landing_orchestrator.handlers import (
    HandlerPrLandingOrchestrator,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models import (
    EnumPrLandingAgentReason,
    EnumPrLandingState,
    ModelPrLandingTransitioned,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.core import (
    PrLandingOrchestratorConfig,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    InMemoryPrLandingRowStore,
)
from omnimarket.nodes.node_pr_landing_reducer.handlers.handler_pr_landing_reducer import (
    HandlerPrLandingReducer,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    ModelPrLifecycleFixCommand,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_head_check_verdict import (
    EnumHeadCheckVerdict,
)
from tests.chains.chain_assert import ChainRecorder, assert_chain
from tests.unit.nodes.node_pr_landing_orchestrator._builders import (
    HEAD_1,
    KEY,
    PR,
    REPO,
    ArmingGate,
    FixedClassifier,
    answer,
    check_run,
    later,
    merged,
    only_request,
    pr_fact,
    prompt,
)

pytestmark = pytest.mark.unit

_CONFIG = PrLandingOrchestratorConfig(
    github_mode=EnumPrLandingGithubMode.ENFORCE,
    companion_exempt_repos=frozenset({REPO}),
)


def _topic_for(event: BaseModel) -> str:
    if isinstance(event, ModelPrLandingGithubRequest):
        return PR_LANDING_GITHUB_REQUESTED_TOPIC_V1
    if isinstance(event, ModelPrLifecycleFixCommand):
        return OCC_AUTOBIND_COMMAND_TOPIC_V1
    return PR_LANDING_EVENT_TOPICS[type(event)]


class _Rig:
    """The orchestrator plus a recorder of its landing events and its requests."""

    def __init__(self, *, verdict: EnumHeadCheckVerdict, arming: bool) -> None:
        self.bus = EventBusInmemory()
        self.recorder = ChainRecorder(self.bus)
        self.requests: list[ModelPrLandingGithubRequest] = []
        self.handler = HandlerPrLandingOrchestrator(
            reducer=HandlerPrLandingReducer(),
            arm_gate=ArmingGate() if arming else HandlerPrArmGate(),
            classifier=FixedClassifier(verdict),
            config=_CONFIG,
            store=InMemoryPrLandingRowStore(),
        )

    async def feed(self, message: BaseModel) -> list[BaseModel]:
        emitted = await self.handler.handle(message)  # type: ignore[arg-type]
        for event in emitted:
            if isinstance(event, ModelPrLandingGithubRequest):
                self.requests.append(event)
                continue
            if type(event) in PR_LANDING_EVENT_TOPICS:
                await self.recorder.publish(
                    _topic_for(event), event, correlation_id=KEY
                )
        return emitted


def _types(names: Sequence[str]) -> list[str]:
    return list(names)


async def test_golden_chain_prompt_to_merged_terminal() -> None:
    rig = _Rig(verdict=EnumHeadCheckVerdict.GREEN, arming=True)
    read = only_request(await rig.feed(prompt()))
    head_read = only_request(await rig.feed(answer(read, pr_state=pr_fact())))
    arm = only_request(
        await rig.feed(answer(head_read, check_runs=(check_run("ci", 7, 70),)))
    )
    assert arm.operation is EnumPrLandingGithubOperation.ARM_AUTO_MERGE
    assert arm.head_sha == HEAD_1
    await rig.feed(answer(arm))
    await rig.feed(merged(later(20)))
    # AC2: a duplicate merged (a second producer, or at-least-once redelivery)
    # adds nothing: exactly one terminal for the episode.
    assert await rig.feed(merged(later(21), event_id="merged-duplicate")) == []

    events = rig.recorder.events
    transitions = [
        e.payload for e in events if isinstance(e.payload, ModelPrLandingTransitioned)
    ]
    assert [t.trigger for t in transitions] == [
        "pushed",
        "evaluated_checks_required",
        "verdict_green",
        "armed_confirmed",
        "merged",
    ]
    assert [t.seq for t in transitions] == [1, 2, 3, 4, 5]
    assert_chain(
        events,
        expected_event_types=_types(
            [ModelPrLandingTransitioned.__name__] * 5 + ["ModelPrLandingMerged"]
        ),
        terminal_fields={"repository": REPO, "pr_number": PR, "episode": 0, "seq": 5},
        correlation_id=KEY,
        bus_history_count=await rig.recorder.bus_history_count(),
    )
    assert [r.operation for r in rig.requests] == [
        EnumPrLandingGithubOperation.READ_PR_STATE,
        EnumPrLandingGithubOperation.READ_HEAD_CHECKS,
        EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
    ]


async def test_agent_chain_real_red_pages_once() -> None:
    rig = _Rig(verdict=EnumHeadCheckVerdict.PRODUCT_FAILED, arming=True)
    read = only_request(await rig.feed(prompt()))
    head_read = only_request(await rig.feed(answer(read, pr_state=pr_fact())))
    await rig.feed(answer(head_read))
    assert_chain(
        rig.recorder.events,
        expected_event_types=_types(
            [ModelPrLandingTransitioned.__name__] * 3 + ["ModelPrLandingAgentNeeded"]
        ),
        terminal_fields={
            "reason": EnumPrLandingAgentReason.REAL_RED,
            "from_state": EnumPrLandingState.CHECKS_PENDING,
            "head_sha": HEAD_1,
        },
        correlation_id=KEY,
        bus_history_count=await rig.recorder.bus_history_count(),
    )


async def test_withheld_arm_chain_stops_at_ready_with_no_arm_request() -> None:
    """The real arm gate under its default policy (report_only, kill switch)."""
    rig = _Rig(verdict=EnumHeadCheckVerdict.GREEN, arming=False)
    read = only_request(await rig.feed(prompt()))
    head_read = only_request(await rig.feed(answer(read, pr_state=pr_fact())))
    await rig.feed(answer(head_read))
    assert_chain(
        rig.recorder.events,
        expected_event_types=_types([ModelPrLandingTransitioned.__name__] * 3),
        terminal_fields={"to_state": EnumPrLandingState.READY, "intents": ()},
        correlation_id=KEY,
        bus_history_count=await rig.recorder.bus_history_count(),
    )
    assert all(
        r.operation is not EnumPrLandingGithubOperation.ARM_AUTO_MERGE
        for r in rig.requests
    )
