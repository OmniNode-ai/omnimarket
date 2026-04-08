"""Workflow Runner — wires the hostile reviewer FSM to the review orchestrator.

Drives the FSM through its phases: INIT -> DISPATCH_REVIEWS -> AGGREGATE ->
CONVERGENCE_CHECK -> REPORT -> DONE. Each phase transition is explicit.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_hostile_reviewer.handlers.handler_hostile_reviewer import (
    HandlerHostileReviewer,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_review_orchestrator import (
    ModelInferenceAdapter,
    ModelOrchestratorInput,
    ModelOrchestratorOutput,
    run_review_orchestration,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_start_command import (
    ModelHostileReviewerStartCommand,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_state import (
    EnumHostileReviewerPhase,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_review_finding import (
    EnumReviewVerdict,
)


class ModelWorkflowInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    diff_content: str = Field(...)
    model_keys: list[str] = Field(...)
    model_context_windows: dict[str, int] = Field(...)
    prompt_template_id: str = Field(default="adversarial_reviewer_pr")
    persona_markdown: str | None = Field(default=None)
    dry_run: bool = Field(default=False)


class ModelWorkflowOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    final_phase: EnumHostileReviewerPhase = Field(...)
    orchestrator_output: ModelOrchestratorOutput | None = Field(default=None)
    pass_count: int = Field(default=0)
    total_findings: int = Field(default=0)
    error_message: str | None = Field(default=None)


async def run_hostile_review_workflow(
    input_data: ModelWorkflowInput,
    inference_adapter: ModelInferenceAdapter,
) -> ModelWorkflowOutput:
    """Run the hostile reviewer workflow end-to-end."""
    fsm = HandlerHostileReviewer()

    command = ModelHostileReviewerStartCommand(
        correlation_id=input_data.correlation_id,
        models=input_data.model_keys,
        dry_run=input_data.dry_run,
        requested_at=datetime.now(tz=UTC),
    )
    state = fsm.start(command)

    # INIT -> DISPATCH_REVIEWS
    state, _ = fsm.advance(state, phase_success=True)

    # DISPATCH_REVIEWS: run orchestrator
    orch_output: ModelOrchestratorOutput | None = None
    try:
        orch_output = await run_review_orchestration(
            ModelOrchestratorInput(
                correlation_id=input_data.correlation_id,
                diff_content=input_data.diff_content,
                model_keys=input_data.model_keys,
                model_context_windows=input_data.model_context_windows,
                prompt_template_id=input_data.prompt_template_id,
                persona_markdown=input_data.persona_markdown,
            ),
            inference_adapter=inference_adapter,
        )
        finding_count = len(orch_output.merged_findings)
        is_clean = orch_output.verdict == EnumReviewVerdict.CLEAN

        # DISPATCH_REVIEWS -> AGGREGATE
        state, _ = fsm.advance(
            state, phase_success=True, findings=finding_count, is_clean_pass=is_clean
        )

        # AGGREGATE -> CONVERGENCE_CHECK
        state, _ = fsm.advance(state, phase_success=True)

        # CONVERGENCE_CHECK -> REPORT
        state, _ = fsm.advance(state, phase_success=True)

        # REPORT -> DONE
        state, _ = fsm.advance(state, phase_success=True)

    except Exception as e:
        state, _ = fsm.advance(state, phase_success=False, error_message=str(e))

    return ModelWorkflowOutput(
        correlation_id=input_data.correlation_id,
        final_phase=state.current_phase,
        orchestrator_output=orch_output,
        pass_count=state.pass_count,
        total_findings=state.total_findings,
        error_message=state.error_message,
    )


class HandlerWorkflowRunner:
    """RuntimeLocal handler protocol wrapper for workflow runner."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Delegates to run_hostile_review_workflow. Requires an inference_adapter
        to be injected via set_adapter() before calling handle().
        """
        parsed = ModelWorkflowInput(**input_data)
        if self._adapter is None:
            msg = "inference_adapter not set — call set_adapter() first"
            raise RuntimeError(msg)
        result = asyncio.run(run_hostile_review_workflow(parsed, self._adapter))
        return result.model_dump(mode="json")

    def __init__(self) -> None:
        self._adapter: ModelInferenceAdapter | None = None

    def set_adapter(self, adapter: ModelInferenceAdapter) -> None:
        """Inject the inference adapter before calling handle()."""
        self._adapter = adapter


__all__: list[str] = [
    "HandlerWorkflowRunner",
    "ModelWorkflowInput",
    "ModelWorkflowOutput",
    "run_hostile_review_workflow",
]
