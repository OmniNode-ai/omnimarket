# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J-recover drill (OMN-22960, rebuild plan section 4.1): the runtime is down, a fix still lands.

The drill stops the shared runtime and then runs the two triggers that matter for a recovery:
the landing tick merges a fix, and the dev-merge redeploy trigger publishes the redeploy-start
command for that merge. Both must work with no runtime, because the runtime is the thing being
redeployed.

What "depends on the runtime" means here. A bus handle that a runtime-hosted consumer holds is
dead when the runtime is down: it cannot subscribe, its deliveries stop and its publishes raise.
A trigger built on such a handle publishes no start command and fails the drill. The contract
carries the same fact: a node with an ``event_bus`` block or ``handler_routing`` is attached to
the shared runtime by auto-wiring, while the redeploy trigger declares neither and is served by
one process on a lab host (its own contract says so).
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    event_type_for,
)
from omnimarket.lab_work.bus import _bytes
from omnimarket.models.dev_merge_redeploy_overlay import ModelDevMergeRedeployPolicy
from omnimarket.models.landing_decision import ModelLandingAction
from omnimarket.models.model_repo_merged import ModelRepoMerged
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers import (
    DevMergeRedeployHost,
    HandlerDevMergeRedeployTrigger,
    HandlerDevMergeRedeployVerify,
    load_dev_merge_redeploy_topics,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    EnumTickStatus,
    ModelTickRequest,
    ModelTickSnapshot,
)
from tests.unit.dev_merge_redeploy.support import (
    Bus,
    FakeBuildContext,
    FakeClassifier,
    FakeFiles,
    FakeLane,
    wait_for,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
TRIGGER_CONTRACT = (
    REPO_ROOT
    / "src/omnimarket/nodes/node_dev_merge_redeploy_trigger_effect/contract.yaml"
)
# Two green pull requests the script's tick merges in one pass (reviewed scenario S22).
FIX_TICK = (
    REPO_ROOT
    / "tests/fixtures/pr_landing_tick_replay/scenario_s22_green_disarmed_hold_release.json"
)
RUNTIME_ATTACHING_BLOCKS = ("event_bus", "handler_routing")


class RuntimeStoppedError(RuntimeError):
    """A call reached the shared runtime while it was stopped."""


class DrillRuntime:
    """The shared runtime, up until the drill stops it."""

    def __init__(self) -> None:
        self.up = True

    def stop(self) -> None:
        self.up = False

    def require_up(self) -> None:
        if not self.up:
            raise RuntimeStoppedError("the shared runtime is stopped")


class RuntimeAttachedBus:
    """The bus as a runtime-hosted consumer sees it: alive only while the runtime is."""

    def __init__(self, bus: Bus, runtime: DrillRuntime) -> None:
        self._bus = bus
        self._runtime = runtime

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> object:
        self._runtime.require_up()
        await self._bus.publish(topic, key, value)
        return None

    async def subscribe(
        self,
        topic: str,
        node_identity: Any = None,
        on_message: Callable[[Any], Awaitable[None]] | None = None,
        *,
        group_id: str | None = None,
    ) -> Callable[[], Awaitable[None]]:
        self._runtime.require_up()
        runtime = self._runtime
        assert on_message is not None
        assert group_id is not None
        deliver = on_message

        async def gated(message: ProtocolBusMessage) -> None:
            if runtime.up:
                await deliver(message)

        return await self._bus.subscribe(topic, gated, group_id)


def attached_to_runtime(contract: dict[str, Any]) -> bool:
    """True when auto-wiring would attach this node to the shared runtime."""
    return any(block in contract for block in RUNTIME_ATTACHING_BLOCKS)


class RecordingMergeWriter:
    """The landing tick's one door to a merge: records the merge, touches no runtime."""

    def __init__(self) -> None:
        self.merged: list[ModelLandingAction] = []

    def perform(self, action: ModelLandingAction) -> None:
        if action.kind.value == "merge":
            self.merged.append(action)


def fix_policy(repo: str) -> ModelDevMergeRedeployPolicy:
    return ModelDevMergeRedeployPolicy.model_validate(
        {
            "repos": [
                {
                    "repo": repo,
                    "source_repo": "infra",
                    "base_branches": ["dev"],
                    "clone_dir": "$OMNI_HOME/sibling",
                    "build_context_clone_dir": "$OMNI_HOME/build-context",
                    "build_context_branch": "dev",
                    "provenance_section": "siblings",
                }
            ],
            "classifier_path": "$OMNI_HOME/gate/classifier.py",
            "lane_probe_argv": ["probe"],
            "requested_by": "lab-node",
            "mode": "publish",
            "observe_poll_seconds": 0.02,
            "verify_deadline_seconds": 5.0,
        }
    )


@dataclass
class DrillResult:
    merges_by_tick: int = 0
    commands: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def _drill_repo(subject: str) -> tuple[str, str]:
    """The fixture's subjects name a synthetic owner; the policy only admits the real one."""
    name, _, number = subject.rpartition("#")
    return f"OmniNode-ai/{name.rpartition('/')[2]}", number


def _merge_events(writer: RecordingMergeWriter) -> list[ModelRepoMerged]:
    events: list[ModelRepoMerged] = []
    for action in writer.merged:
        repo, number = _drill_repo(action.subject)
        events.append(
            ModelRepoMerged(
                repo=repo,
                base="dev",
                merge_sha=action.head_sha,
                pr_number=int(number),
                merged_at=datetime.now(UTC),
                source="j-recover-drill",
            )
        )
    return events


async def run_drill(
    trigger_contract: dict[str, Any],
    attach: Callable[[Bus, DrillRuntime], Any],
) -> DrillResult:
    """Stop the runtime, merge a fix through the landing tick, and see if the redeploy fires."""
    result = DrillResult()
    runtime = DrillRuntime()
    bus = Bus()
    snapshot = ModelTickSnapshot.model_validate_json(FIX_TICK.read_text("utf-8"))
    repo, _ = _drill_repo(snapshot.decision.actions[0].subject)
    policy = fix_policy(repo)
    topics = load_dev_merge_redeploy_topics()
    await bus.start()
    host = DevMergeRedeployHost(
        attach(bus, runtime),
        HandlerDevMergeRedeployTrigger(
            policy,
            files=FakeFiles(),
            classifier=FakeClassifier(),
            build_context=FakeBuildContext(),
        ),
        HandlerDevMergeRedeployVerify(policy, lane=FakeLane()),
        policy,
        host_name="h-drill",
    )

    async def record(message: ProtocolBusMessage) -> None:
        result.commands.append(json.loads(message.value)["payload"])

    await bus.subscribe(topics.start_command, record, "drill-recorder")
    try:
        await host.start()
        runtime.stop()

        writer = RecordingMergeWriter()
        tick = HandlerPrLandingTickOrchestrator(writer=writer, act_enabled=True).handle(
            ModelTickRequest(
                correlation_id=uuid4(),
                mode=EnumTickMode.ACT,
                facts=snapshot.facts,
                recorded_decision=snapshot.decision,
                fair_share=snapshot.fair_share,
            )
        )
        if tick.status is not EnumTickStatus.COMPARED:
            result.failures.append(f"landing tick did not run: {tick.failure_reason}")
        result.merges_by_tick = len(writer.merged)
        if not writer.merged:
            result.failures.append("the landing tick merged nothing")

        for event in _merge_events(writer):
            envelope = ModelEventEnvelope[dict[str, object]](
                payload=event.model_dump(mode="json"),
                event_type=event_type_for(topics.merged),
            )
            await bus.publish(topics.merged, event.repo.encode(), _bytes(envelope))
        try:
            await wait_for(lambda: len(result.commands) >= 1, timeout=0.5)
        except AssertionError:
            result.failures.append("no redeploy-start command was published")
    except RuntimeStoppedError as exc:
        result.failures.append(f"redeploy trigger needs the runtime: {exc}")
    finally:
        if attached_to_runtime(trigger_contract):
            result.failures.append(
                "redeploy trigger contract attaches to the shared runtime "
                f"({', '.join(b for b in RUNTIME_ATTACHING_BLOCKS if b in trigger_contract)})"
            )
        runtime.up = True
        await host.stop()
        await bus.close()
    return result


def _trigger_contract() -> dict[str, Any]:
    loaded = yaml.safe_load(TRIGGER_CONTRACT.read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _served_by_lab_host(bus: Bus, runtime: DrillRuntime) -> Bus:
    return bus


def _hosted_by_runtime(bus: Bus, runtime: DrillRuntime) -> RuntimeAttachedBus:
    return RuntimeAttachedBus(bus, runtime)


def test_runtime_down_a_fix_lands_and_the_redeploy_trigger_fires() -> None:
    result = asyncio.run(run_drill(_trigger_contract(), _served_by_lab_host))

    assert result.failures == []
    assert result.passed
    assert result.merges_by_tick == 2
    assert len(result.commands) == 2
    assert {c["requested_by"] for c in result.commands} == {
        "lab-node/infra/pr-1869",
        "lab-node/infra/pr-1871",
    }
    assert all(c["runtime_lane"] == "dev" for c in result.commands)


def test_dependent_trigger_fails_the_drill() -> None:
    contract = copy.deepcopy(_trigger_contract())
    contract["event_bus"] = {
        "subscribe_topics": ["onex.evt.omnimarket.repo-merged.v1"],
        "publish_topics": ["onex.cmd.omnimarket.redeploy-start.v1"],
    }
    contract["handler_routing"] = {"routing_strategy": "payload_type_match"}

    result = asyncio.run(run_drill(contract, _hosted_by_runtime))

    assert not result.passed
    assert result.commands == []
    assert result.merges_by_tick == 2  # the tick still lands; the trigger is what fails
    assert "no redeploy-start command was published" in result.failures
    assert any("attaches to the shared runtime" in f for f in result.failures)


def test_dependent_trigger_fails_on_the_runtime_alone_with_a_clean_contract() -> None:
    result = asyncio.run(run_drill(_trigger_contract(), _hosted_by_runtime))

    assert not result.passed
    assert result.commands == []
    assert result.failures == ["no redeploy-start command was published"]


def test_the_shipped_trigger_contract_declares_no_runtime_attachment() -> None:
    contract = _trigger_contract()

    assert not attached_to_runtime(contract)
    assert contract["name"] == "node_dev_merge_redeploy_trigger_effect"
