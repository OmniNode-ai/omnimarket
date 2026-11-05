# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The existing ``ProtocolWorkflowOrchestrator`` of omnibase_spi, presented over the run engine handler.

The protocol returns ``ProtocolResult`` with no field for an accepted, duplicate or unknown start; the outcome travels
as the result's message (the handler result as JSON) and as ``error_code`` for a duplicate or unknown outcome, until
the spi protocol carries a typed outcome. A run is the protocol's scenario; its id is the run key.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from omnimarket.nodes.node_run_engine_effect.handlers.handler_run_engine import (
    HandlerRunEngine,
)
from omnimarket.nodes.node_run_engine_effect.models.model_run_engine import (
    EnumCancelOutcome,
    EnumReleaseOutcome,
    EnumRunEngineOperation,
    EnumRunStatus,
    EnumStartOutcome,
    ModelRunEngineRequest,
    ModelRunEngineResult,
    ModelRunSpec,
)

WORKFLOW_TYPE = "run_engine"

_STATE_FOR_STATUS = {
    EnumRunStatus.RUNNING: "running",
    EnumRunStatus.WAITING: "paused",
    EnumRunStatus.COMPLETED: "completed",
    EnumRunStatus.FAILED: "failed",
    EnumRunStatus.CANCELLED: "cancelled",
}


def instance_id_for(run_key: str) -> UUID:
    """The stable instance id of a run key."""
    return uuid5(NAMESPACE_URL, f"run-engine:{run_key}")


@dataclass(frozen=True)
class SpiResult:
    """Structurally a ``ProtocolResult``."""

    success: bool
    message: str
    result_data: Any
    error_code: str | None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class SpiExecutionState:
    """Structurally a ``ProtocolWorkflowExecutionState``."""

    workflow_type: str
    instance_id: UUID
    state: str
    current_step: int
    total_steps: int
    started_at: datetime
    updated_at: datetime
    context: dict[str, Any]
    execution_metadata: dict[str, Any]

    async def validate_execution_state(self) -> bool:
        return 0 <= self.current_step <= self.total_steps

    def is_completed(self) -> bool:
        return self.state == "completed"


@dataclass(frozen=True)
class SpiHealth:
    """Structurally a ``ProtocolHealthCheckResult``."""

    node_id: str
    node_type: str
    status: str
    timestamp: datetime
    response_time_ms: float | None
    error_message: str | None
    metadata: dict[str, Any]

    async def validate_health_result(self) -> bool:
        return True

    def is_healthy(self) -> bool:
        return self.status == "healthy"


def _spi_result(result: ModelRunEngineResult) -> SpiResult:
    outcome = result.start_outcome or result.release_outcome or result.cancel_outcome
    bad = {
        EnumStartOutcome.DUPLICATE,
        EnumStartOutcome.UNKNOWN,
        EnumReleaseOutcome.DUPLICATE,
        EnumReleaseOutcome.STALE,
        EnumReleaseOutcome.UNKNOWN_RUN,
        EnumReleaseOutcome.RUN_FINISHED,
        EnumReleaseOutcome.UNKNOWN,
        EnumCancelOutcome.DUPLICATE,
        EnumCancelOutcome.UNKNOWN_RUN,
        EnumCancelOutcome.UNKNOWN,
    }
    code = (
        None if outcome is None or outcome not in bad else f"run_engine.{outcome.value}"
    )
    return SpiResult(
        success=code is None,
        message=result.model_dump_json(),
        result_data=None,
        error_code=code,
    )


class AdapterSpiOrchestrator:
    """``ProtocolWorkflowOrchestrator`` over the run engine handler."""

    def __init__(self, handler: HandlerRunEngine) -> None:
        self._handler = handler
        self._registry: object | None = None
        self._started: dict[str, datetime] = {}

    def set_registry(self, registry: object) -> None:
        self._registry = registry

    async def run(self, input_state: Any) -> SpiResult:
        """``input_data['request']`` is a ``ModelRunEngineRequest`` as JSON."""
        raw = input_state.input_data.get("request")
        if not isinstance(raw, str):
            return SpiResult(
                False,
                "input_data.request must be a JSON string",
                None,
                "run_engine.bad_input",
            )
        request = ModelRunEngineRequest.model_validate_json(raw)
        result = await self._handler.handle(request)
        if request.run is not None:
            self._started.setdefault(request.run.run_key, datetime.now(UTC))
        return _spi_result(result)

    async def orchestrate_operation(
        self,
        operation_type: str,
        scenario_id: str,
        correlation_id: UUID,
        parameters: Any,
    ) -> SpiResult:
        """``operation_type`` is a run engine operation; ``scenario_id`` is the run key."""
        params = parameters.parameters
        op = EnumRunEngineOperation(operation_type)
        request = ModelRunEngineRequest(
            operation=op,
            run=(
                ModelRunSpec.model_validate_json(str(params["run"]))
                if op is EnumRunEngineOperation.START_RUN
                else None
            ),
            run_key=None
            if op in (EnumRunEngineOperation.START_RUN, EnumRunEngineOperation.HEALTH)
            else scenario_id,
            condition=str(params["condition"]) if "condition" in params else None,
            release_id=str(params["release_id"]) if "release_id" in params else None,
        )
        return await self.run(
            _InputState(WORKFLOW_TYPE, {"request": request.model_dump_json()})
        )

    async def get_execution_state(self, scenario_id: str) -> SpiExecutionState | None:
        result = await self._handler.handle(
            ModelRunEngineRequest(
                operation=EnumRunEngineOperation.GET_RUN_STATE, run_key=scenario_id
            )
        )
        state = result.state
        if state is None or state.status is EnumRunStatus.NOT_FOUND:
            return None
        now = datetime.now(UTC)
        return SpiExecutionState(
            workflow_type=WORKFLOW_TYPE,
            instance_id=instance_id_for(scenario_id),
            state=_STATE_FOR_STATUS[state.status],
            current_step=len(state.recorded),
            total_steps=state.total_steps,
            started_at=self._started.get(scenario_id, now),
            updated_at=now,
            context={"waiting_on": state.waiting_on or ""},
            execution_metadata={"backend": result.backend, "detail": state.detail},
        )

    async def health_check(self) -> SpiHealth:
        began = time.monotonic()
        result = await self._handler.handle(
            ModelRunEngineRequest(operation=EnumRunEngineOperation.HEALTH)
        )
        healthy = result.healthy is True
        return SpiHealth(
            node_id="node_run_engine_effect",
            node_type="EFFECT",
            status="healthy" if healthy else "unhealthy",
            timestamp=datetime.now(UTC),
            response_time_ms=(time.monotonic() - began) * 1000.0,
            error_message=None if healthy else result.detail,
            metadata={"backend": result.backend},
        )


@dataclass(frozen=True)
class _InputState:
    workflow_type: str
    input_data: dict[str, Any]
    parameters: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    async def validate_workflow_input(self) -> bool:
        return True
