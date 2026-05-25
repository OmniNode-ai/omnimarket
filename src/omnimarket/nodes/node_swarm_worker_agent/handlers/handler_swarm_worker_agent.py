# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Worker agent handler — receives a subtask assignment, calls local model endpoint, publishes result."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol

from omnimarket.models.swarm.enum_execution_status import EnumExecutionStatus
from omnimarket.models.swarm.model_swarm_subtask_assignment import (
    ModelSwarmSubtaskAssignment,
)
from omnimarket.models.swarm.model_swarm_subtask_result import ModelSwarmSubtaskResult
from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_swarm_worker_agent.models.model_worker_agent_request import (
    ModelWorkerAgentRequest,
)
from omnimarket.nodes.node_swarm_worker_agent.models.model_worker_agent_result import (
    ModelWorkerAgentResult,
)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"


class _HttpResponse(Protocol):
    def json(self) -> dict[str, object]: ...

    @property
    def status_code(self) -> int: ...


class ProtocolHttpClient(Protocol):
    def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
    ) -> _HttpResponse: ...


class ProtocolResultPublisher(Protocol):
    def publish(self, topic: str, payload: dict[str, object]) -> None: ...


class HandlerSwarmWorkerAgent:
    def __init__(
        self,
        http_client: ProtocolHttpClient | None = None,
        result_publisher: ProtocolResultPublisher | None = None,
    ) -> None:
        self._http_client = http_client
        self._result_publisher = result_publisher

    def handle(self, request: ModelWorkerAgentRequest) -> ModelWorkerAgentResult:
        import httpx

        client: ProtocolHttpClient = self._http_client or httpx.Client()
        publish_topics = contract_publish_topics(_CONTRACT_PATH)
        completion_topic = next(
            t for t in publish_topics if "swarm-subtask-completed" in t
        )

        result = self._execute(
            assignment=request.assignment,
            worker_id=request.worker_id,
            client=client,
        )

        if self._result_publisher is not None:
            self._result_publisher.publish(completion_topic, result.model_dump())

        return ModelWorkerAgentResult(
            subtask_result=result,
            published_topic=completion_topic,
        )

    def _execute(
        self,
        assignment: ModelSwarmSubtaskAssignment,
        worker_id: str,
        client: ProtocolHttpClient,
    ) -> ModelSwarmSubtaskResult:
        t0 = time.monotonic()

        # Skip if this assignment is not for this worker (empty worker_id = accept all)
        if assignment.worker_id and assignment.worker_id != worker_id:
            return ModelSwarmSubtaskResult(
                run_id=assignment.run_id,
                subtask_id=assignment.subtask_id,
                worker_id=worker_id,
                execution_status=EnumExecutionStatus.SKIPPED_DEPENDENCY_FAILED,
                failure_reason="worker_id_mismatch",
                model_id=assignment.model_id,
            )

        try:
            response = client.post(
                f"{assignment.endpoint_url}/chat/completions",
                json={
                    "model": assignment.model_id,
                    "messages": [{"role": "user", "content": assignment.description}],
                },
                timeout=float(assignment.timeout_seconds),
            )
            data: dict[str, object] = response.json()
            choices = data.get("choices", [])
            text = ""
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    msg = first.get("message", {})
                    if isinstance(msg, dict):
                        text = str(msg.get("content", ""))
            latency_ms = int((time.monotonic() - t0) * 1000)
            return ModelSwarmSubtaskResult(
                run_id=assignment.run_id,
                subtask_id=assignment.subtask_id,
                worker_id=worker_id,
                execution_status=EnumExecutionStatus.SUCCEEDED,
                response_text=text,
                latency_ms=latency_ms,
                model_id=assignment.model_id,
            )
        except TimeoutError:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return ModelSwarmSubtaskResult(
                run_id=assignment.run_id,
                subtask_id=assignment.subtask_id,
                worker_id=worker_id,
                execution_status=EnumExecutionStatus.TIMEOUT,
                failure_reason="endpoint_timeout",
                latency_ms=latency_ms,
                model_id=assignment.model_id,
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return ModelSwarmSubtaskResult(
                run_id=assignment.run_id,
                subtask_id=assignment.subtask_id,
                worker_id=worker_id,
                execution_status=EnumExecutionStatus.FAILED,
                failure_reason=str(exc),
                latency_ms=latency_ms,
                model_id=assignment.model_id,
            )
