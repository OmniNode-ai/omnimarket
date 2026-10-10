# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-process calls the landing orchestrator makes, as protocols (OMN-19829).

Plan section 5: the orchestrator calls three pure nodes in process, never over
the bus: the landing reducer (T6), the head-check classifier (T3/T8) and the arm
gate. Each is a protocol here so the orchestrator is tested against the real
handler or an explicit double, and so the handler can be composed on the lab
(wave 3) without changing this node.

Each default resolves the real handler: the reducer by import when first
used, the arm gate and the head-check classifier (:mod:`.head_checks`,
OMN-20866) directly. A handler that is not in the tree is a typed refusal of
the message that needed it, never a guess.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Awaitable
from typing import Protocol, runtime_checkable

from omnimarket.events.pr_arm_gate import ModelArmGateDecision, ModelArmGateRequest
from omnimarket.events.pr_head_check.model_head_check_verdict import (
    ModelHeadCheckVerdict,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_completed import (
    ModelPrLandingGithubCompleted,
)
from omnimarket.events.pr_landing_reduce import (
    ModelPrLandingReduceInput,
    ModelPrLandingReduceOutput,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_workflow_row import (
    ModelPrLandingWorkflowRow,
)

_REDUCER_MODULE = (
    "omnimarket.nodes.node_pr_landing_reducer.handlers.handler_pr_landing_reducer"
)
_REDUCER_CLASS = "HandlerPrLandingReducer"


class PrLandingPortUnavailableError(RuntimeError):
    """A pure node the orchestrator calls in process is not present in this build."""


@runtime_checkable
class ProtocolPrLandingReducer(Protocol):
    """The pure definition-B reducer: row plus observation to next row and intents."""

    def handle(
        self, request: ModelPrLandingReduceInput
    ) -> ModelPrLandingReduceOutput | Awaitable[ModelPrLandingReduceOutput]: ...


@runtime_checkable
class ProtocolPrLandingArmGate(Protocol):
    """node_pr_arm_gate_compute's handler: ARM or WITHHOLD, fail-closed."""

    async def handle(self, request: ModelArmGateRequest) -> ModelArmGateDecision: ...


@runtime_checkable
class ProtocolPrLandingHeadCheckClassifier(Protocol):
    """Turns one read_head_checks answer into the PR-level verdict for its head.

    The classification itself is ``classify_head_checks`` on
    node_pr_lifecycle_triage_compute (T3, T8); :mod:`.head_checks` builds its
    facts from the effect's check runs, run attempts and required contexts
    (T9, OMN-20866) and is the handler's default.
    """

    async def classify(
        self,
        completed: ModelPrLandingGithubCompleted,
        row: ModelPrLandingWorkflowRow,
    ) -> ModelHeadCheckVerdict: ...


async def call_reducer(
    reducer: ProtocolPrLandingReducer, request: ModelPrLandingReduceInput
) -> ModelPrLandingReduceOutput:
    """Call a reducer whether its ``handle`` is synchronous or a coroutine."""
    result = reducer.handle(request)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, ModelPrLandingReduceOutput):
        msg = (
            f"reducer returned {type(result).__name__}, not ModelPrLandingReduceOutput"
        )
        raise TypeError(msg)
    return result


class LazyPrLandingReducer:
    """Resolves node_pr_landing_reducer's handler on first use (T6)."""

    def __init__(self) -> None:
        self._reducer: ProtocolPrLandingReducer | None = None

    def _resolve(self) -> ProtocolPrLandingReducer:
        if self._reducer is None:
            try:
                module = importlib.import_module(_REDUCER_MODULE)
            except ModuleNotFoundError as exc:
                msg = (
                    "node_pr_landing_reducer has no handler in this build; the "
                    "landing orchestrator cannot move a row without it"
                )
                raise PrLandingPortUnavailableError(msg) from exc
            handler = getattr(module, _REDUCER_CLASS)()
            if not isinstance(handler, ProtocolPrLandingReducer):
                msg = f"{_REDUCER_CLASS} does not implement handle()"
                raise PrLandingPortUnavailableError(msg)
            self._reducer = handler
        return self._reducer

    def handle(
        self, request: ModelPrLandingReduceInput
    ) -> ModelPrLandingReduceOutput | Awaitable[ModelPrLandingReduceOutput]:
        return self._resolve().handle(request)


__all__: list[str] = [
    "LazyPrLandingReducer",
    "PrLandingPortUnavailableError",
    "ProtocolPrLandingArmGate",
    "ProtocolPrLandingHeadCheckClassifier",
    "ProtocolPrLandingReducer",
    "call_reducer",
]
