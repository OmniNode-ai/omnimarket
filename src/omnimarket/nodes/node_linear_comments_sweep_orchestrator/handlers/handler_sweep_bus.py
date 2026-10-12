# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sweep's two bus boundaries on the canonical request-response wiring.

Model calls ride the delegation orchestrator's request and reply topics; each
decision rides the Linear-comments decision compute node's command and terminal
topics. No model HTTP clients, subprocess executors or consumers live here: the
infra runtime owns publishing, correlation matching and bounded replies.
"""

from __future__ import annotations

import asyncio
import json
import os
from importlib.resources import files
from typing import cast
from uuid import UUID, uuid5

import yaml
from omnibase_core.models.contracts.subcontracts import ModelRequestResponseConfig
from omnibase_core.models.delegation.wire import ModelDelegationRequest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)
from pydantic import JsonValue

from ..models.model_linear_comments_sweep import (
    ModelLinearCommentsSweepRequest,
    ModelSweepPhaseReply,
    ModelSweepPhaseRequest,
)
from ..protocols.protocol_sweep_rpc import ProtocolSweepRpc

PACKAGE = "omnimarket.nodes.node_linear_comments_sweep_orchestrator"
PHASE_INSTANCE = "sweep_phase"
DECISION_INSTANCE = "sweep_decision"


def _int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _optional_int(payload: dict[str, object], key: str) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class HandlerSweepBus:
    """Phase and decision gateways over infra's existing request-response service."""

    def __init__(
        self,
        event_bus: ProtocolEventBusPublisher | None = None,
        rpc: ProtocolSweepRpc | None = None,
    ) -> None:
        self._rpc = rpc
        self._event_bus = event_bus
        self._lock = asyncio.Lock()
        self._instances: dict[str, dict[str, JsonValue]] | None = None
        self._decisions_asked = 0

    def _declared(self, name: str) -> dict[str, JsonValue]:
        if self._instances is None:
            contract = yaml.safe_load(
                files(PACKAGE).joinpath("contract.yaml").read_text()
            )
            declared = cast(
                list[dict[str, JsonValue]],
                contract["event_bus"]["request_response"]["instances"],
            )
            self._instances = {str(i["name"]): i for i in declared}
        return self._instances[name]

    async def _wiring(self) -> ProtocolSweepRpc:
        async with self._lock:
            if self._rpc is None:
                if self._event_bus is None:
                    raise ValueError(
                        "linear comments sweep requires the runtime event_bus"
                    )
                from omnibase_infra.runtime.request_response_wiring import (
                    RequestResponseWiring,
                )

                wiring = RequestResponseWiring(
                    self._event_bus,
                    environment=os.environ["ONEX_ENVIRONMENT"],
                    app_name="linear-comments-sweep",
                )
                await wiring.wire_request_response(
                    ModelRequestResponseConfig.model_validate(
                        {"instances": list(self._all_instances())}
                    )
                )
                self._rpc = wiring
            return self._rpc

    def _all_instances(self) -> list[dict[str, JsonValue]]:
        return [self._declared(PHASE_INSTANCE), self._declared(DECISION_INSTANCE)]

    @staticmethod
    def _payload_of(
        response: dict[str, object], correlation: UUID
    ) -> dict[str, object]:
        payload = response.get("payload", response)
        if not isinstance(payload, dict):
            raise ValueError("sweep terminal payload must be an object")
        seen = payload.get("correlation_id", response.get("correlation_id"))
        if str(seen) != str(correlation):
            raise ValueError("sweep terminal correlation does not match")
        return cast(dict[str, object], payload)

    async def run(
        self, run: ModelLinearCommentsSweepRequest, phase: ModelSweepPhaseRequest
    ) -> ModelSweepPhaseReply:
        instance = self._declared(PHASE_INSTANCE)
        correlation = uuid5(run.correlation_id, phase.label)
        command = ModelDelegationRequest(
            correlation_id=correlation,
            tenant_id=run.tenant_id,
            emitted_at=run.emitted_at,
            prompt=phase.prompt,
            task_type=phase.task_type,
            max_tokens=phase.max_tokens,
            requested_timeout_seconds=(
                phase.timeout_seconds if phase.task_type == "agent_delegation" else None
            ),
            context_pack=json.dumps(
                {"phase": phase.phase, "model": phase.model, "effort": phase.effort}
            ),
            response_format={"type": "json_object"},
            response_contract=cast(dict[str, object] | None, phase.schema_definition),
        )
        envelope = ModelEventEnvelope[ModelDelegationRequest](
            payload=command,
            correlation_id=correlation,
            tenant_id=run.tenant_id,
            event_type=str(instance["request_topic"]),
            envelope_timestamp=run.emitted_at,
        )
        wiring = await self._wiring()
        response = await wiring.send_request(
            PHASE_INSTANCE,
            envelope.model_dump(mode="json"),
            timeout_seconds=phase.timeout_seconds,
        )
        payload = self._payload_of(response, correlation)
        if payload.get("tenant_id", run.tenant_id) != run.tenant_id:
            raise ValueError("sweep terminal tenant does not match")
        content = payload.get("content")
        failure = payload.get("failure_reason")
        text = content if isinstance(content, str) else ""
        reason = failure if isinstance(failure, str) else ""
        model_used = payload.get("model_used")
        finish = payload.get("finish_reason")
        truncated = payload.get("truncated")
        return ModelSweepPhaseReply(
            ok=bool(text.strip()) and not reason,
            failure=reason
            or ("" if text.strip() else "the delegation returned no content"),
            content=text,
            model_used=model_used if isinstance(model_used, str) else "",
            finish_reason=finish if isinstance(finish, str) else None,
            truncated=truncated if isinstance(truncated, bool) else None,
            prompt_tokens=_int(payload, "prompt_tokens"),
            completion_tokens=_int(payload, "completion_tokens"),
            total_tokens=_int(payload, "total_tokens"),
            latency_ms=_int(payload, "latency_ms"),
            attempts=_optional_int(payload, "attempts_count"),
            escalations=_optional_int(payload, "escalation_count"),
        )

    async def decide(
        self, run: ModelLinearCommentsSweepRequest, request: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        instance = self._declared(DECISION_INSTANCE)
        self._decisions_asked += 1
        correlation = uuid5(
            run.correlation_id, f"decision:{self._decisions_asked}:{request['kind']}"
        )
        envelope = ModelEventEnvelope[dict[str, JsonValue]](
            payload=request,
            correlation_id=correlation,
            tenant_id=run.tenant_id,
            event_type=str(instance["request_topic"]),
            envelope_timestamp=run.emitted_at,
        )
        wiring = await self._wiring()
        response = await wiring.send_request(
            DECISION_INSTANCE,
            envelope.model_dump(mode="json"),
            timeout_seconds=int(cast(int, instance["timeout_seconds"])),
        )
        payload = self._payload_of(response, correlation)
        if payload.get("kind") != request["kind"]:
            raise ValueError(
                f"decision {request['kind']}: the answer is for {payload.get('kind')}"
            )
        return cast(dict[str, JsonValue], payload)
