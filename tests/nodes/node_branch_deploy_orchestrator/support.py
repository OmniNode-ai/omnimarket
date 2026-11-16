# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The other side of every topic the branch deploy orchestrator speaks to, for its tests (OMN-24027).

``Plant`` answers each command the orchestrator publishes: the real command effect over an in-memory bus,
the real probe effect over an in-memory probe, and the real reducer fold. Nothing here decides for the
orchestrator: it only routes what was published to the node that owns the topic and hands back what that
node answered.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import yaml

from omnimarket.models.model_dev_deploy_reading import ModelRuntimeProbeFact
from omnimarket.nodes.node_branch_deploy_command_effect.handlers.handler_branch_deploy_command import (
    HandlerBranchDeployCommand,
)
from omnimarket.nodes.node_branch_deploy_command_effect.models.model_branch_deploy_command import (
    ModelBranchDeployCommandRequest,
)
from omnimarket.nodes.node_branch_deploy_command_effect.overlay import (
    ModelBranchDeployCommandOverlay,
)
from omnimarket.nodes.node_branch_deploy_orchestrator.handlers.handler_branch_deploy_orchestrator import (
    HandlerBranchDeployOrchestrator,
)
from omnimarket.nodes.node_branch_deploy_orchestrator.models import (
    ModelBranchDeployCompleted,
    ModelBranchDeployRequest,
    ModelDeployJobCompleted,
    ModelDeployJobRejected,
)
from omnimarket.nodes.node_branch_deploy_orchestrator.overlay import (
    ModelBranchDeployOrchestratorOverlay,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.adapters.adapter_in_memory_probe import (
    InMemoryBranchDeployProbe,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.handlers.handler_branch_deploy_probe import (
    HandlerBranchDeployProbe,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.models.model_branch_deploy_probe import (
    ModelBranchDeployProbeRequest,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.overlay import (
    ModelBranchDeployProbeOverlay,
)
from omnimarket.nodes.node_host_install_state_reducer.handlers.handler_host_install_state import (
    HostInstallTransitionError,
    fold,
)
from omnimarket.nodes.node_host_install_state_reducer.models import (
    ModelHostInstallRecord,
    ModelHostInstallReduceCommand,
)
from omnimarket.nodes.node_host_install_state_reducer.overlay import (
    ModelHostInstallStateOverlay,
)
from tests.nodes.node_branch_deploy_command_effect.support import (
    SIGNING_ENV,
    SIGNING_KEY,
    FailingBus,
    FakeBus,
    matching_reader,
)

NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
NODE_DIR = NODES / "node_branch_deploy_orchestrator"
GOLDEN = Path(__file__).resolve().parent / "deploy_agent_job_events.json"
START = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
HEAD = "a" * 40
ROLLBACK_HEAD = "b" * 40
OTHER = "c" * 40
IMAGE = "onex-boot-good-example:0123456789ab"


def _document(node: str) -> dict[str, object]:
    loaded = yaml.safe_load((NODES / node / "overlay.example.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def orchestrator_overlay() -> ModelBranchDeployOrchestratorOverlay:
    return ModelBranchDeployOrchestratorOverlay.model_validate(
        _document("node_branch_deploy_orchestrator")
    )


def command_overlay() -> ModelBranchDeployCommandOverlay:
    overlay = ModelBranchDeployCommandOverlay.model_validate(
        _document("node_branch_deploy_command_effect")
    )
    assert overlay.hmac_secret_env == SIGNING_ENV
    return overlay


def probe_overlay() -> ModelBranchDeployProbeOverlay:
    return ModelBranchDeployProbeOverlay.model_validate(
        _document("node_branch_deploy_probe_effect")
    )


def reducer_overlay() -> ModelHostInstallStateOverlay:
    return ModelHostInstallStateOverlay.model_validate(
        _document("node_host_install_state_reducer")
    )


def request(correlation_id: UUID | None = None) -> ModelBranchDeployRequest:
    return ModelBranchDeployRequest(
        correlation_id=correlation_id or uuid4(),
        host="host-a",
        unit="dev-runtime-hotpatch",
        actor="lane-test",
        runtime_lane="dev",
        repo="omnibase_infra",
        pr_number=5917,
        head_sha=HEAD,
        rollback_head_sha=ROLLBACK_HEAD,
        rollback_image=IMAGE,
        rollback_reason="hot patch of a pull request head",
    )


def job_event(
    kind: str, correlation_id: UUID
) -> ModelDeployJobCompleted | ModelDeployJobRejected:
    raw = yaml.safe_load(GOLDEN.read_text())
    assert isinstance(raw, dict)
    document = raw[kind]
    assert isinstance(document, dict)
    document["correlation_id"] = str(correlation_id)
    if kind == "rejected":
        return ModelDeployJobRejected.model_validate(document)
    return ModelDeployJobCompleted.model_validate(document)


def probe_fact(
    observed: str | None, ok: bool, expected_sha: str = HEAD
) -> ModelRuntimeProbeFact:
    return ModelRuntimeProbeFact(
        probe_id="probe-test",
        probed_at=START,
        expected_sha=expected_sha,
        observed_sha=observed,
        ok=ok,
    )


class StepClock:
    """A clock that moves one second per read, so every step event has its own time."""

    def __init__(self) -> None:
        self.ticks = 0

    def __call__(self) -> datetime:
        self.ticks += 1
        return START + timedelta(seconds=self.ticks)


def published() -> str:
    return "published"


def refused(message: str = "bad") -> str:
    return "refused"


def failed() -> str:
    return "failed"


def read(observed: str, ok: bool, expected: str = HEAD):
    """One probe answer: a fact the runtime reported."""
    return ((probe_fact(observed, ok, expected_sha=expected),), None)


UNREADABLE = ((), "runtime probe unreadable: ValueError")


class Plant:
    """Routes every published payload to the node that owns its topic and queues the answer.

    The deploy agent is the one stand-in: ``job_events`` are the terminal events it publishes after the command
    effect published its rebuild command, in order (``completed_success``, ``completed_failed`` or ``rejected``).
    """

    def __init__(
        self,
        *,
        outcomes: list[str] | None = None,
        probes: list[Any] | None = None,
        job_events: list[str] | None = None,
    ) -> None:
        self.orchestrator = HandlerBranchDeployOrchestrator(
            orchestrator_overlay(), StepClock()
        )
        self.outcomes = list(outcomes or [])
        self.probe = InMemoryBranchDeployProbe(facts=list(probes or []))
        self.job_events: list[str] = list(job_events or [])
        self.requests: list[ModelBranchDeployCommandRequest] = []
        self.envelopes: list[dict[str, Any]] = []
        self.probe_handler = HandlerBranchDeployProbe(
            overlay=probe_overlay(), probe=self.probe, clock=lambda: START
        )
        self.reducer_overlay = reducer_overlay()
        self.log: list[tuple[str, Any]] = []
        self.records: list[ModelHostInstallRecord] = []
        self.reducer_errors: list[str] = []
        self.completed: ModelBranchDeployCompleted | None = None

    @property
    def commands(self) -> list[dict[str, Any]]:
        """The envelopes the command effect published, in order; a refused or failed command publishes none."""
        return self.envelopes

    def run(self, start: ModelBranchDeployRequest) -> None:
        queue: deque[tuple[object, UUID]] = deque([(start, start.correlation_id)])
        while queue:
            assert len(queue) <= 200, "the FIFO cap of 200 items was hit"
            payload, key = queue.popleft()
            for out in self.orchestrator.decide(payload, key):
                self.log.append((out.topic, out.payload))
                for answer in self._serve(out.topic, out.payload, key):
                    queue.append((answer, key))

    def _serve(self, topic: str, payload: Any, key: UUID) -> list[Any]:
        if topic.endswith("branch-deploy-command-requested.v1"):
            assert isinstance(payload, ModelBranchDeployCommandRequest)
            self.requests.append(payload)
            outcome = self.outcomes.pop(0)
            bus: FakeBus | FailingBus = (
                FailingBus() if outcome == "failed" else FakeBus()
            )
            environ = {} if outcome == "refused" else {SIGNING_ENV: SIGNING_KEY}
            handler = HandlerBranchDeployCommand(
                bus,
                overlay=command_overlay(),
                environ=environ,
                key_check_reader=matching_reader(),
            )
            result = asyncio.run(handler.handle(payload))
            if isinstance(bus, FakeBus):
                self.envelopes.extend(
                    json.loads(value) for _, _, value in bus.published
                )
            if result.status == "published":
                return [result, job_event(self.job_events.pop(0), key)]
            return [result]
        if topic.endswith("branch-deploy-probe-requested.v1"):
            assert isinstance(payload, ModelBranchDeployProbeRequest)
            return [self.probe_handler.handle(payload)]
        if topic.endswith("host-install-step.v1"):
            assert isinstance(payload, ModelHostInstallReduceCommand)
            try:
                record = fold(payload.prior, payload.event, self.reducer_overlay)
            except HostInstallTransitionError as refusal:
                self.reducer_errors.append(str(refusal))
                return []
            self.records.append(record)
            return [record]
        if topic.endswith("branch-deploy-completed.v1"):
            assert isinstance(payload, ModelBranchDeployCompleted)
            self.completed = payload
            return []
        raise AssertionError(f"the orchestrator published on an unknown topic {topic}")


#: The five flows of the orchestrator, by name: what the deploy agent publishes, what the runtime reports, what the command effect answers.
FLOWS: dict[str, dict[str, Any]] = {
    "golden": {
        "outcomes": [published()],
        "job_events": ["completed_success"],
        "probes": [read(HEAD, True)],
    },
    "wrong_build": {
        "outcomes": [published(), published()],
        "job_events": ["completed_success", "completed_success"],
        "probes": [read(OTHER, True), read(ROLLBACK_HEAD, True, ROLLBACK_HEAD)],
    },
    "unhealthy": {
        "outcomes": [published(), published()],
        "job_events": ["completed_success", "completed_success"],
        "probes": [read(HEAD, False), read(ROLLBACK_HEAD, True, ROLLBACK_HEAD)],
    },
    "unreadable": {
        "outcomes": [published(), published()],
        "job_events": ["completed_success", "completed_success"],
        "probes": [UNREADABLE, read(ROLLBACK_HEAD, True, ROLLBACK_HEAD)],
    },
    "refused": {"outcomes": [refused()]},
}


def run_flow(name: str) -> Plant:
    plant = Plant(**{k: list(v) for k, v in FLOWS[name].items()})
    plant.run(request())
    return plant
