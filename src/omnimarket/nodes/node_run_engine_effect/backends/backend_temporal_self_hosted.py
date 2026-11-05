# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The self-hosted Temporal overlay of the run engine.

The run's state is read from the workflow's own history, never from a copy: recorded steps are the completed
activities and the signals that released waits, in the order of the run spec. That reading works with no worker
running, which is the case after a worker death. The server is the user's own process on the lab host; the node
connects to the address the overlay names and to nothing else.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from omnimarket.nodes.node_run_engine_effect.backends.protocol_run_engine_backend import (
    RunEngineBackendUnavailableError,
    decide_release,
)
from omnimarket.nodes.node_run_engine_effect.backends.temporal_names import (
    SIGNAL_NAME,
    WORKFLOW_NAME,
)
from omnimarket.nodes.node_run_engine_effect.models.model_run_engine import (
    EnumCancelOutcome,
    EnumReleaseOutcome,
    EnumRunStatus,
    EnumRunStepKind,
    EnumStartOutcome,
    ModelRunSpec,
    ModelRunState,
    ModelRunStepRecord,
    ModelTemporalSelfHostedSettings,
)

_TERMINAL = (EnumRunStatus.COMPLETED, EnumRunStatus.FAILED, EnumRunStatus.CANCELLED)


class BackendTemporalSelfHosted:
    """Drives runs on a self-hosted Temporal server through its Python client."""

    def __init__(self, settings: ModelTemporalSelfHostedSettings) -> None:
        self._settings = settings
        self._client: Any = None

    @property
    def name(self) -> str:
        return "temporal_self_hosted"

    @property
    def durable(self) -> bool:
        return True

    async def _connect(self) -> Any:
        from temporalio.client import Client

        if self._client is not None:
            return self._client
        try:
            self._client = await asyncio.wait_for(
                Client.connect(
                    self._settings.target_host, namespace=self._settings.namespace
                ),
                timeout=self._settings.connect_timeout_seconds,
            )
        except (TimeoutError, OSError, RuntimeError) as exc:
            raise RunEngineBackendUnavailableError(
                f"temporal server not reachable ({type(exc).__name__})"
            ) from None
        return self._client

    def _unavailable(self, exc: Exception) -> RunEngineBackendUnavailableError:
        self._client = None
        return RunEngineBackendUnavailableError(
            f"temporal call failed ({type(exc).__name__})"
        )

    async def start_run(self, spec: ModelRunSpec) -> EnumStartOutcome:
        from temporalio.client import WorkflowFailureError
        from temporalio.common import WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError
        from temporalio.service import RPCError

        client = await self._connect()
        try:
            await client.start_workflow(
                WORKFLOW_NAME,
                args=[
                    spec.model_dump_json(),
                    self._settings.model_dump_json(),
                ],
                id=spec.run_key,
                task_queue=self._settings.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            return EnumStartOutcome.DUPLICATE
        except (RPCError, WorkflowFailureError, OSError) as exc:
            raise self._unavailable(exc) from None
        return EnumStartOutcome.ACCEPTED

    async def release_run(
        self, run_key: str, condition: str, release_id: str
    ) -> EnumReleaseOutcome:
        from temporalio.service import RPCError, RPCStatusCode

        state = await self.get_run_state(run_key)
        decided = decide_release(state, condition, release_id)
        if decided is not None:
            return decided
        client = await self._connect()
        try:
            await client.get_workflow_handle(run_key).signal(
                SIGNAL_NAME,
                json.dumps({"condition": condition, "release_id": release_id}),
            )
        except RPCError as exc:
            if exc.status is RPCStatusCode.NOT_FOUND:
                return EnumReleaseOutcome.UNKNOWN_RUN
            raise self._unavailable(exc) from None
        except OSError as exc:
            raise self._unavailable(exc) from None
        return EnumReleaseOutcome.ACCEPTED

    async def cancel_run(self, run_key: str) -> EnumCancelOutcome:
        from temporalio.service import RPCError, RPCStatusCode

        state = await self.get_run_state(run_key)
        if state.status is EnumRunStatus.NOT_FOUND:
            return EnumCancelOutcome.UNKNOWN_RUN
        if state.status in _TERMINAL:
            return EnumCancelOutcome.DUPLICATE
        client = await self._connect()
        try:
            await client.get_workflow_handle(run_key).cancel()
        except RPCError as exc:
            if exc.status is RPCStatusCode.NOT_FOUND:
                return EnumCancelOutcome.UNKNOWN_RUN
            raise self._unavailable(exc) from None
        except OSError as exc:
            raise self._unavailable(exc) from None
        return EnumCancelOutcome.ACCEPTED

    async def get_run_state(self, run_key: str) -> ModelRunState:
        from temporalio.api.enums.v1 import EventType
        from temporalio.service import RPCError, RPCStatusCode

        client = await self._connect()
        handle = client.get_workflow_handle(run_key)
        try:
            events = [event async for event in handle.fetch_history_events()]
        except RPCError as exc:
            if exc.status is RPCStatusCode.NOT_FOUND:
                return ModelRunState(run_key=run_key, status=EnumRunStatus.NOT_FOUND)
            raise self._unavailable(exc) from None
        except OSError as exc:
            raise self._unavailable(exc) from None
        converter = client.data_converter
        spec: ModelRunSpec | None = None
        scheduled: dict[int, str] = {}
        completed: dict[str, str] = {}
        released: dict[str, str] = {}
        closed: EnumRunStatus | None = None
        detail = ""
        for event in events:
            kind = event.event_type
            if kind == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
                payloads = (
                    event.workflow_execution_started_event_attributes.input.payloads
                )
                values = await converter.decode(payloads, [str, str])
                spec = ModelRunSpec.model_validate_json(values[0])
            elif kind == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
                attrs = event.activity_task_scheduled_event_attributes
                values = await converter.decode(attrs.input.payloads, [str, str])
                scheduled[event.event_id] = json.loads(values[1])["name"]
            elif kind == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
                attrs = event.activity_task_completed_event_attributes
                name = scheduled[attrs.scheduled_event_id]
                values = await converter.decode(attrs.result.payloads, [str])
                completed[name] = values[0]
            elif kind == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED:
                attrs = event.workflow_execution_signaled_event_attributes
                if attrs.signal_name == SIGNAL_NAME:
                    values = await converter.decode(attrs.input.payloads, [str])
                    body = json.loads(values[0])
                    released.setdefault(body["condition"], body["release_id"])
            elif kind == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED:
                closed = EnumRunStatus.COMPLETED
            elif kind == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED:
                closed = EnumRunStatus.FAILED
                detail = (
                    event.workflow_execution_failed_event_attributes.failure.message
                )
            elif kind == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED:
                closed = EnumRunStatus.CANCELLED
            elif kind in (
                EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED,
                EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT,
            ):
                closed = EnumRunStatus.FAILED
                detail = "terminated or timed out"
        if spec is None:
            return ModelRunState(run_key=run_key, status=EnumRunStatus.NOT_FOUND)
        recorded: list[ModelRunStepRecord] = []
        waiting_on: str | None = None
        for step in spec.steps:
            if step.kind is EnumRunStepKind.COMMAND:
                if step.name not in completed:
                    break
                recorded.append(
                    ModelRunStepRecord(
                        name=step.name, kind=step.kind, output=completed[step.name]
                    )
                )
            else:
                condition = step.condition or ""
                if condition not in released:
                    waiting_on = condition
                    break
                recorded.append(
                    ModelRunStepRecord(
                        name=step.name,
                        kind=step.kind,
                        condition=condition,
                        released_by=released[condition],
                    )
                )
        if closed is not None:
            status = closed
            waiting_on = None
        else:
            status = (
                EnumRunStatus.WAITING
                if waiting_on is not None
                else EnumRunStatus.RUNNING
            )
        return ModelRunState(
            run_key=run_key,
            status=status,
            total_steps=len(spec.steps),
            recorded=tuple(recorded),
            waiting_on=waiting_on,
            detail=detail,
        )

    async def settle(self, run_key: str, timeout_seconds: float) -> ModelRunState:
        deadline = time.monotonic() + timeout_seconds
        state = await self.get_run_state(run_key)
        while time.monotonic() < deadline:
            if state.status in (*_TERMINAL, EnumRunStatus.WAITING):
                return state
            await asyncio.sleep(0.25)
            state = await self.get_run_state(run_key)
        return state

    async def health(self) -> bool:
        from temporalio.service import RPCError

        try:
            client = await self._connect()
            await client.service_client.check_health()
        except (
            RunEngineBackendUnavailableError,
            RPCError,
            OSError,
            RuntimeError,
        ):
            self._client = None
            return False
        return True
