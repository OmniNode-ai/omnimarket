# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus-bound harness delegation handler of the delegation call effect (OMN-26219).

``handle(request) -> result`` over ``ModelHarnessBackendCallRequest``. The ports are
built on first use from the lane overlay's bound dependencies, so auto-wiring can
construct this handler unbound and an unbound overlay refuses at the first call, not
at start-up (the same shape as ``HandlerDelegationTarget``). Which harness backends
exist, their models and their order come from the delegation routing overlay; the
Claude Code environment and credential references come from the ``delegation_adapters``
document, under the call site this contract's ``harness_backend_call`` block names.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from importlib import resources
from typing import Any, Literal

import yaml

from omnimarket.handlers.container.container_overlay_dependencies import (
    declared_handler_dependencies,
    overlay_documents,
    process_environment,
    require_dependencies,
)
from omnimarket.nodes.node_llm_delegation_call_effect.codex_harness_policy import (
    PACKAGE,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_codex_harness_attempt import (
    HandlerCodexHarnessAttempt,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
    load_delegation_config,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_answer_gate import (
    HandlerHarnessAnswerGate,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_backend_call import (
    HandlerHarnessBackendCall,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_process_runner import (
    HandlerHarnessProcessRunner,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_lab_agent_local_rung import (
    HandlerLabAgentLocalRung,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_harness_backend_call import (
    ModelHarnessAttemptRecord,
    ModelHarnessBackendCallRequest,
    ModelHarnessBackendCallResult,
)

logger = logging.getLogger(__name__)

_CONTRACT_BLOCK = "harness_backend_call"


def _contract_block() -> dict[str, Any]:
    contract = yaml.safe_load(
        resources.files(PACKAGE).joinpath("contract.yaml").read_text()
    )
    block = contract[_CONTRACT_BLOCK]
    assert isinstance(block, dict)
    return block


class HandlerHarnessCostPublisher:
    """Publishes the one cost record each attempt leaves, on the topic the contract declares."""

    def __init__(self, topic: str) -> None:
        self._topic = topic

    def record(self, attempt: ModelHarnessAttemptRecord) -> None:
        from omnimarket.events.emit_effect_topic_publisher import (
            EmitEffectTopicPublisher,
        )

        key = str(attempt.request_id)
        EmitEffectTopicPublisher().publish(
            topic=self._topic,
            event_type="delegation.harness.attempt.recorded",
            payload=attempt.model_dump(mode="json"),
            event_id=f"{key}:{attempt.attempt}",
            correlation_id=key,
            partition_key=key,
        )


class HandlerHarnessDelegation:
    """Canonical typed handler: one delegation through the declared harness backends."""

    def __init__(
        self,
        event_bus: Any = None,
        dependencies: Mapping[str, object] | None = None,
        call: HandlerHarnessBackendCall | None = None,
    ) -> None:
        self._bus = event_bus
        self._dependencies = dependencies
        self._call = call

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def _bind(self) -> HandlerHarnessBackendCall:
        if self._call is None:
            bound = (
                self._dependencies
                if self._dependencies is not None
                else require_dependencies(declared_handler_dependencies(PACKAGE))
            )
            documents = overlay_documents(bound)
            block = _contract_block()
            config = load_delegation_config(documents)
            site = config["call_sites"][block["agents_call_site"]]
            self._call = HandlerHarnessBackendCall(
                runner=HandlerHarnessProcessRunner(
                    environment=process_environment(bound),
                    agents=site.get("agents", {}),
                    settings=documents,
                    codex=HandlerCodexHarnessAttempt(dependencies=bound),
                    local_rung=HandlerLabAgentLocalRung(),
                ),
                gate=HandlerHarnessAnswerGate(),
                cost_sink=HandlerHarnessCostPublisher(block["attempt_topic"]),
            )
        return self._call

    async def handle(
        self, request: ModelHarnessBackendCallRequest
    ) -> ModelHarnessBackendCallResult:
        return await self._bind().handle(request)


__all__ = ["HandlerHarnessCostPublisher", "HandlerHarnessDelegation"]
