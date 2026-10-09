# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Use the canonical delegation request-response wiring for morning phase work.

No model HTTP clients, subprocess executors, or consumer implementation here.
The infra runtime owns publishing, correlation matching, and bounded replies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from importlib.resources import files
from typing import cast
from uuid import uuid5

from omnibase_core.models.contracts.subcontracts import ModelRequestResponseConfig
from omnibase_core.models.delegation.wire import ModelDelegationRequest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)
from pydantic import BaseModel, JsonValue

from ..models import model_phase_results as results
from ..models.model_morning_ground_state import (
    ModelMorningGroundStateRequest,
    ModelMorningPhaseRequest,
)
from ..models.model_morning_overlay import ModelMorningOverlay
from ..protocols.protocol_morning_rpc import ProtocolMorningRpc

logger = logging.getLogger(__name__)

PACKAGE = "omnimarket.nodes.node_morning_ground_state_orchestrator"

RESULT_TYPES: dict[str, type[BaseModel]] = {
    "idempotency-precheck": results.ModelIdempotencyPrecheckResult,
    "decisions-register": results.ModelDecisionsRegisterResult,
    "ground-state": results.ModelGroundStateResult,
    "morning-triage": results.ModelMorningTriageResult,
    "plan-reconcile": results.ModelPlanReconcileResult,
    "integration-plan": results.ModelIntegrationPlanResult,
    "dropped-work": results.ModelDroppedWorkResult,
    "session-goal": results.ModelSessionGoalResult,
}


class HandlerMorningPhaseBus:
    """A phase gateway using infra's existing RPC service and delegation topics."""

    def __init__(
        self,
        event_bus: ProtocolEventBusPublisher | None = None,
        rpc: ProtocolMorningRpc | None = None,
        overlay: ModelMorningOverlay | None = None,
    ) -> None:
        self._rpc = rpc
        self._overlay = overlay
        self._event_bus = event_bus
        self._lock = asyncio.Lock()

    def _packaged_contract(self) -> dict[str, JsonValue]:
        import yaml

        return cast(
            dict[str, JsonValue],
            yaml.safe_load(files(PACKAGE).joinpath("contract.yaml").read_text()),
        )

    def _request_topic(self) -> str:
        event_bus = cast(dict[str, JsonValue], self._packaged_contract()["event_bus"])
        instances = cast(
            list[dict[str, JsonValue]],
            cast(dict[str, JsonValue], event_bus["request_response"])["instances"],
        )
        return str(instances[0]["request_topic"])

    async def _wiring(self) -> ProtocolMorningRpc:
        async with self._lock:
            if self._rpc is None:
                if self._event_bus is None:
                    raise ValueError(
                        "morning ground-state requires the runtime event_bus"
                    )
                import yaml
                from omnibase_infra.runtime.request_response_wiring import (
                    RequestResponseWiring,
                )

                contract = yaml.safe_load(
                    files("omnimarket.nodes.node_morning_ground_state_orchestrator")
                    .joinpath("contract.yaml")
                    .read_text()
                )
                wiring = RequestResponseWiring(
                    self._event_bus,
                    environment=os.environ["ONEX_ENVIRONMENT"],
                    app_name="morning-ground-state",
                )
                await wiring.wire_request_response(
                    ModelRequestResponseConfig.model_validate(
                        cast(dict[str, JsonValue], contract["event_bus"])[
                            "request_response"
                        ]
                    )
                )
                self._rpc = wiring
            return self._rpc

    async def run(
        self, run: ModelMorningGroundStateRequest, phase: ModelMorningPhaseRequest
    ) -> dict[str, JsonValue] | None:
        correlation = uuid5(run.correlation_id, phase.label)
        command = ModelDelegationRequest(
            correlation_id=correlation,
            tenant_id=run.tenant_id,
            emitted_at=run.emitted_at,
            prompt=phase.prompt,
            task_type="agent_delegation",
            max_tokens=8192,
            requested_timeout_seconds=14400,
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
            event_type=self._request_topic(),
            envelope_timestamp=run.emitted_at,
        )
        wiring = await self._wiring()
        response = await wiring.send_request(
            "morning_phase", envelope.model_dump(mode="json"), timeout_seconds=14400
        )
        payload = response.get("payload", response)
        if not isinstance(payload, dict):
            raise ValueError("morning phase terminal payload must be an object")
        if str(payload.get("correlation_id")) != str(correlation):
            raise ValueError("morning phase terminal correlation does not match")
        if payload.get("tenant_id", run.tenant_id) != run.tenant_id:
            raise ValueError("morning phase terminal tenant does not match")
        content = payload.get("content")
        if not isinstance(content, str):
            raise ValueError("morning phase terminal contains no content")
        decoded = json.loads(content)
        if decoded is None:
            return None
        if phase.label in RESULT_TYPES:
            # Validate against the ported typed models, preserving explicit null
            # and omitted optional fields when the downstream brief reads JSON.
            RESULT_TYPES[phase.label].model_validate(decoded)
        if not isinstance(decoded, dict):
            raise ValueError("morning phase result must be an object")
        return cast(dict[str, JsonValue], decoded)

    async def reconcile(self, run: ModelMorningGroundStateRequest) -> None:
        from .handler_morning_ground_state import load_morning_overlay

        overlay = self._overlay or load_morning_overlay()
        live_lanes = run.live_lanes or []
        if any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", lane) for lane in live_lanes
        ):
            logger.warning(
                "ledger-reconcile skipped: invalid live lane name; roster must be complete"
            )
            return
        reconcile_args = (
            ("--apply" if run.publish else "--report")
            + " --stale-hours 6 --since-days 0 --max-appends 60 --json"
            + "".join(f" --live-lane {lane}" for lane in live_lanes)
        )
        if run.live_lanes is not None:
            reconcile_args += " --live-roster --silent-hours 72"
        prompt = (
            "Run the canonical ledger reconciliation node through its bus adapter:\n"
            + overlay.texts["ledger_reconcile_command"]
            + " "
            + reconcile_args
            + "\nReturn its typed JSON terminal event verbatim. Exit 1 means dangling claims or "
            "stale holds were reported or reconciled; exits 2, 3 and 4 mean blocked. "
            "Do not modify or commit anything yourself. The node owns verification, holds, "
            "caps and appends. abandoned counts silent handle-free claims whose completion "
            "is unverified; auto_closed counts landed claims only. ROUTE: "
            "band=B2 model=sonnet effort=medium"
        )
        await self.run(
            run,
            ModelMorningPhaseRequest(
                label="ledger-reconcile",
                phase="LedgerReconcile",
                model="sonnet",
                effort="medium",
                prompt=prompt,
                schema_definition={"type": "object"},
            ),
        )
