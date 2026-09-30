# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The kernel publishes what the landing orchestrator emits (OMN-20127).

The seam test drives the handler through omnibase_core's ``RuntimeDispatch``,
which publishes a bare ``list`` return. The compose runtime does not: it wires
this node through omnibase_infra ``handler_wiring``, whose
``_normalize_handler_result`` drops a bare sequence unless the def-B fan-out
seam flag is set, and the state_io in-row outbox then captures nothing. On the
.201 dev lane that left every pr_landing_workflow_state row at UNSEEN with a
GitHub read recorded in flight and nothing on the request topic.

These tests go through the two functions the kernel itself calls: the one that
picks the entrypoint, and the one that turns its return into output events,
with the seam flag unset as it is on every lane.
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    ENV_MULTI_EVENT_PUBLISH_SEAM,
    _normalize_handler_result,
    _resolve_effective_handle_method,
)

from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubOperation,
    ModelPrLandingGithubRequest,
)
from omnimarket.nodes.node_pr_landing_orchestrator.handlers import (
    HandlerPrLandingOrchestrator,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    InMemoryPrLandingRowStore,
)
from omnimarket.nodes.node_pr_landing_reducer.handlers.handler_pr_landing_reducer import (
    HandlerPrLandingReducer,
)
from tests.unit.nodes.node_pr_landing_orchestrator._builders import (
    ArmingGate,
    FixedClassifier,
    prompt,
)

pytestmark = pytest.mark.unit


def _handler() -> HandlerPrLandingOrchestrator:
    return HandlerPrLandingOrchestrator(
        reducer=HandlerPrLandingReducer(),
        arm_gate=ArmingGate(),
        classifier=FixedClassifier(),
        store=InMemoryPrLandingRowStore(),
    )


async def _kernel_output_events(
    handler: HandlerPrLandingOrchestrator, message: object
) -> list[object]:
    entrypoint = _resolve_effective_handle_method(handler)
    assert entrypoint is not None
    result = entrypoint(message)
    if hasattr(result, "__await__"):
        result = await result
    dispatch = _normalize_handler_result(
        result,
        message,
        type(message).__name__,
        handler_node_kind=EnumNodeKind.ORCHESTRATOR,
    )
    assert dispatch is not None
    return list(dispatch.output_events)


def test_the_handler_class_declares_the_runtime_entrypoint() -> None:
    assert "handle_async" in HandlerPrLandingOrchestrator.__dict__


@pytest.mark.asyncio
async def test_handle_async_a_push_prompt_reaches_the_kernel_as_one_read_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_MULTI_EVENT_PUBLISH_SEAM, raising=False)
    message = prompt()

    events = await _kernel_output_events(_handler(), message)

    assert [type(event) for event in events] == [ModelPrLandingGithubRequest]
    request = events[0]
    assert isinstance(request, ModelPrLandingGithubRequest)
    assert request.operation is EnumPrLandingGithubOperation.READ_PR_STATE
    assert request.pr_number == message.pr_number


@pytest.mark.asyncio
async def test_handle_async_a_dropped_message_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_MULTI_EVENT_PUBLISH_SEAM, raising=False)
    handler = _handler()
    first = prompt()
    await _kernel_output_events(handler, first)

    # A second prompt while the read is in flight is folded without an effect.
    events = await _kernel_output_events(handler, prompt())

    assert all(not isinstance(e, ModelPrLandingGithubRequest) for e in events)
