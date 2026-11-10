# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The default reducer of the landing orchestrator handler (OMN-23600).

The runtime auto-wiring builds ``HandlerPrLandingOrchestrator()`` with no
arguments, so the reducer the first leg reaches is the one the container
binds. These tests hold that seam: a handler built with no ``reducer``
argument reduces through the real node_pr_landing_reducer handler (a prompt's
read answer transitions, the withheld-arm chain stops at READY), a handler
whose reducer is left explicitly unwired refuses with the typed error and
leaves the row untouched, and the classes that cross the seam are the ones
the reducer node's contract names.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import BaseModel

from omnimarket.events.pr_landing_reduce import (
    ModelPrLandingReduceInput,
    ModelPrLandingReduceOutput,
)
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
from omnimarket.nodes.node_pr_landing_orchestrator.container.container_inmemory import (
    bind_reducer,
)
from omnimarket.nodes.node_pr_landing_orchestrator.event_topics import (
    PR_LANDING_EVENT_TOPICS,
)
from omnimarket.nodes.node_pr_landing_orchestrator.handlers import (
    HandlerPrLandingOrchestrator,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models import (
    EnumPrLandingState,
    ModelPrLandingTransitioned,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.core import (
    PrLandingOrchestratorConfig,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.ports import (
    LazyPrLandingReducer,
    PrLandingPortUnavailableError,
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
    KEY,
    REPO,
    FixedClassifier,
    answer,
    check_run,
    only_request,
    pr_fact,
    prompt,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[4]
_REDUCER_CONTRACT = _ROOT / "src/omnimarket/nodes/node_pr_landing_reducer/contract.yaml"

# The checks path: this repo needs no companion, so evaluation reads the checks.
_ENFORCE = PrLandingOrchestratorConfig(
    github_mode=EnumPrLandingGithubMode.ENFORCE,
    companion_exempt_repos=frozenset({REPO}),
)


def _default_handler() -> HandlerPrLandingOrchestrator:
    """The handler as the runtime auto-wiring builds it: no reducer injected."""
    return HandlerPrLandingOrchestrator(
        arm_gate=HandlerPrArmGate(),
        classifier=FixedClassifier(EnumHeadCheckVerdict.GREEN),
        config=_ENFORCE,
        store=InMemoryPrLandingRowStore(),
    )


def _topic_for(event: BaseModel) -> str:
    if isinstance(event, ModelPrLandingGithubRequest):
        return PR_LANDING_GITHUB_REQUESTED_TOPIC_V1
    if isinstance(event, ModelPrLifecycleFixCommand):
        return OCC_AUTOBIND_COMMAND_TOPIC_V1
    return PR_LANDING_EVENT_TOPICS[type(event)]


class _Rig:
    """The default-built orchestrator plus a recorder of its landing events."""

    def __init__(self) -> None:
        self.bus = EventBusInmemory()
        self.recorder = ChainRecorder(self.bus)
        self.requests: list[ModelPrLandingGithubRequest] = []
        self.handler = _default_handler()

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


async def test_default_built_handler_reduces_instead_of_raising() -> None:
    """A handler with no reducer injected runs the bound one: the read answer
    transitions the row instead of refusing it with PrLandingPortUnavailableError."""
    handler = _default_handler()
    read = only_request(await handler.handle(prompt()))
    emitted = await handler.handle(answer(read, pr_state=pr_fact()))
    transitions = [e for e in emitted if isinstance(e, ModelPrLandingTransitioned)]
    assert [t.to_state for t in transitions] == [
        EnumPrLandingState.OBSERVED,
        EnumPrLandingState.CHECKS_PENDING,
    ]


async def test_golden_chain_with_default_reducer() -> None:
    """The withheld-arm chain (real arm gate, report_only) over the default
    binding: pushed, evaluated, verdict_green, and no arm request leaves it READY."""
    rig = _Rig()
    read = only_request(await rig.feed(prompt()))
    head_read = only_request(await rig.feed(answer(read, pr_state=pr_fact())))
    await rig.feed(answer(head_read, check_runs=(check_run("ci", 7, 70),)))
    assert_chain(
        rig.recorder.events,
        expected_event_types=[ModelPrLandingTransitioned.__name__] * 3,
        terminal_fields={"to_state": EnumPrLandingState.READY, "intents": ()},
        correlation_id=KEY,
        bus_history_count=await rig.recorder.bus_history_count(),
    )
    assert all(
        r.operation is not EnumPrLandingGithubOperation.ARM_AUTO_MERGE
        for r in rig.requests
    )


async def test_error_chain_explicitly_unwired_reducer_refuses_with_typed_error() -> (
    None
):
    """A reducer left unwired on purpose still refuses, and the refusal is not
    a write: the row the prompt leg committed is exactly what remains."""
    store = InMemoryPrLandingRowStore()
    handler = HandlerPrLandingOrchestrator(
        reducer=LazyPrLandingReducer(),
        arm_gate=HandlerPrArmGate(),
        classifier=FixedClassifier(EnumHeadCheckVerdict.GREEN),
        config=_ENFORCE,
        store=store,
    )
    read = only_request(await handler.handle(prompt()))
    before, version_before = await store.load(KEY)
    assert before is not None  # the prompt leg committed its read
    with pytest.raises(PrLandingPortUnavailableError):
        await handler.handle(answer(read, pr_state=pr_fact()))
    after, version_after = await store.load(KEY)
    assert (after, version_after) == (before, version_before)


def test_seam_orchestrator_reducer_models() -> None:
    """The default binding is the real reducer handler, and its handle takes
    and returns exactly the models the reducer node's contract names."""
    assert isinstance(bind_reducer(), HandlerPrLandingReducer)
    hints = get_type_hints(HandlerPrLandingReducer.handle)
    (request_parameter,) = [
        parameter
        for name, parameter in inspect.signature(
            HandlerPrLandingReducer.handle
        ).parameters.items()
        if name != "self"
    ]
    assert hints[request_parameter.name] is ModelPrLandingReduceInput
    assert hints["return"] is ModelPrLandingReduceOutput

    contract = yaml.safe_load(_REDUCER_CONTRACT.read_text("utf-8"))
    assert contract["input_model"]["name"] == ModelPrLandingReduceInput.__name__
    assert contract["output_model"]["name"] == ModelPrLandingReduceOutput.__name__

    assert frozenset(ModelPrLandingReduceInput.model_fields) == frozenset(
        {"state", "observation"}
    )
