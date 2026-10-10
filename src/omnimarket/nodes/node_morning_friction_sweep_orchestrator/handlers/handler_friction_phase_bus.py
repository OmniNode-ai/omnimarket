# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Use the canonical delegation request-response wiring for friction sweep phases.

No model HTTP clients, subprocess executors, or consumer implementation here.
The infra runtime owns publishing, correlation matching, and bounded replies.
"""

from __future__ import annotations

import asyncio
import json
import os
from importlib.resources import files
from typing import cast
from uuid import uuid5

import yaml
from omnibase_core.models.contracts.subcontracts import ModelRequestResponseConfig
from omnibase_core.models.delegation.wire import ModelDelegationRequest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)
from pydantic import BaseModel, JsonValue

from ..models import model_friction_phase_results as results
from ..models.model_morning_friction_sweep import (
    ModelFrictionPhaseRequest,
    ModelMorningFrictionSweepRequest,
)
from ..protocols.protocol_friction_rpc import ProtocolFrictionRpc

PACKAGE = "omnimarket.nodes.node_morning_friction_sweep_orchestrator"
INSTANCE = "friction_phase"

RESULT_TYPES: dict[str, type[BaseModel]] = {
    "friction-precheck": results.ModelFrictionPrecheck,
    "friction-scan": results.ModelFrictionScan,
    "friction-source-linear": results.ModelFrictionSource,
    "friction-source-checkpoints": results.ModelFrictionSource,
    "friction-source-ci": results.ModelFrictionSource,
    "friction-source-guards": results.ModelFrictionSource,
    "friction-synthesize": results.ModelFrictionSynthesis,
    "friction-adjudicate": results.ModelFrictionAdjudication,
    "friction-report": results.ModelFrictionReport,
}


class HandlerFrictionPhaseBus:
    """A phase gateway using infra's existing RPC service and delegation topics."""

    def __init__(
        self,
        event_bus: ProtocolEventBusPublisher | None = None,
        rpc: ProtocolFrictionRpc | None = None,
    ) -> None:
        self._rpc = rpc
        self._event_bus = event_bus
        self._lock = asyncio.Lock()
        self._instance: dict[str, JsonValue] | None = None

    def _request_response(self) -> dict[str, JsonValue]:
        contract = yaml.safe_load(files(PACKAGE).joinpath("contract.yaml").read_text())
        return cast(dict[str, JsonValue], contract["event_bus"]["request_response"])

    def _declared_instance(self) -> dict[str, JsonValue]:
        if self._instance is None:
            instances = cast(
                list[dict[str, JsonValue]], self._request_response()["instances"]
            )
            self._instance = next(i for i in instances if i["name"] == INSTANCE)
        return self._instance

    async def _wiring(self) -> ProtocolFrictionRpc:
        async with self._lock:
            if self._rpc is None:
                if self._event_bus is None:
                    raise ValueError("friction sweep requires the runtime event_bus")
                from omnibase_infra.runtime.request_response_wiring import (
                    RequestResponseWiring,
                )

                wiring = RequestResponseWiring(
                    self._event_bus,
                    environment=os.environ["ONEX_ENVIRONMENT"],
                    app_name="morning-friction-sweep",
                )
                await wiring.wire_request_response(
                    ModelRequestResponseConfig.model_validate(self._request_response())
                )
                self._rpc = wiring
            return self._rpc

    async def run(
        self,
        run: ModelMorningFrictionSweepRequest,
        phase: ModelFrictionPhaseRequest,
    ) -> dict[str, JsonValue] | None:
        instance = self._declared_instance()
        timeout = int(cast(int, instance["timeout_seconds"]))
        correlation = uuid5(run.correlation_id, phase.label)
        command = ModelDelegationRequest(
            correlation_id=correlation,
            tenant_id=run.tenant_id,
            emitted_at=run.emitted_at,
            prompt=phase.prompt,
            task_type="agent_delegation",
            max_tokens=8192,
            requested_timeout_seconds=timeout,
            context_pack=json.dumps(
                {"phase": phase.phase, "model": phase.model, "effort": phase.effort}
            ),
            response_format={"type": "json_object"},
            response_contract=cast(dict[str, object], phase.schema_definition),
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
            INSTANCE, envelope.model_dump(mode="json"), timeout_seconds=timeout
        )
        payload = response.get("payload", response)
        if not isinstance(payload, dict):
            raise ValueError("friction phase terminal payload must be an object")
        if str(payload.get("correlation_id")) != str(correlation):
            raise ValueError("friction phase terminal correlation does not match")
        if payload.get("tenant_id", run.tenant_id) != run.tenant_id:
            raise ValueError("friction phase terminal tenant does not match")
        content = payload.get("content")
        if not isinstance(content, str):
            raise ValueError("friction phase terminal contains no content")
        decoded = json.loads(content)
        if decoded is None:
            return None
        if not isinstance(decoded, dict):
            raise ValueError("friction phase result must be an object")
        RESULT_TYPES[phase.label].model_validate(decoded)
        return cast(dict[str, JsonValue], decoded)
