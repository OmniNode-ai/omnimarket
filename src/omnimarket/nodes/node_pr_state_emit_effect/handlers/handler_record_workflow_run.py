# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Learn which workflow a run id belongs to; no check-run delivery carries the workflow name."""

from collections.abc import Mapping
from uuid import NAMESPACE_URL, uuid5

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.github_check_run import (
    ModelGitHubWorkflowRunObservation,
    observation_from_wire,
)
from omnimarket.nodes.node_pr_state_emit_effect.handlers.handler_detect_ci_red import (
    MEMORY_LIMIT,
    PROCESS_INDEX,
    CiRedIndex,
)


class ModelRecordWorkflowRunRequest(BaseModel):
    """Validate the webhook ingress wire payload before runtime handler dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    observation: ModelGitHubWorkflowRunObservation

    @model_validator(mode="before")
    @classmethod
    def from_wire(cls, value: object) -> object:
        if isinstance(value, Mapping) and "observation" not in value:
            return {
                "observation": observation_from_wire(
                    ModelGitHubWorkflowRunObservation, value
                )
            }
        return value


class HandlerRecordWorkflowRun:
    MEMORY_LIMIT = MEMORY_LIMIT

    def __init__(self, *, index: CiRedIndex | None = None) -> None:
        self._index = index if index is not None else PROCESS_INDEX

    async def handle(
        self, request: ModelRecordWorkflowRunRequest | Mapping[str, object]
    ) -> ModelHandlerOutput[None]:
        observation = (
            request
            if isinstance(request, ModelRecordWorkflowRunRequest)
            else ModelRecordWorkflowRunRequest.model_validate(request)
        ).observation
        self._index.learn_workflow(
            observation.repo,
            observation.run_id,
            observation.workflow,
            self.MEMORY_LIMIT,
        )
        correlation_id = uuid5(
            NAMESPACE_URL,
            f"onex:workflow-run:{observation.repo}:{observation.run_id}:{observation.status}",
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id="node_pr_state_emit_effect.record_workflow_run",
            events=(),
        )
