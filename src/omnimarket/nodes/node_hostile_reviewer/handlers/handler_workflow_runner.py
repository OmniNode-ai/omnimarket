"""Workflow Runner — wires the hostile reviewer FSM to the review orchestrator.

Drives the FSM through its phases: INIT -> DISPATCH_REVIEWS -> AGGREGATE ->
CONVERGENCE_CHECK -> REPORT -> DONE. Each phase transition is explicit.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
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

DEFAULT_MODEL_CONTEXT_WINDOW = 32_000
GH_PR_DIFF_TIMEOUT_SECONDS = 30
RUNTIME_LOCAL_METADATA_KEYS = frozenset({"rows", "event_landed", "latency_ms"})


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


def workflow_input_from_start_command(
    command: ModelHostileReviewerStartCommand,
) -> ModelWorkflowInput:
    """Adapt the contract start command into the internal workflow input."""
    return ModelWorkflowInput(
        correlation_id=command.correlation_id,
        diff_content=_resolve_diff_content(command),
        model_keys=command.models,
        model_context_windows=dict.fromkeys(
            command.models, DEFAULT_MODEL_CONTEXT_WINDOW
        ),
        dry_run=command.dry_run,
    )


def _resolve_diff_content(command: ModelHostileReviewerStartCommand) -> str:
    if command.file_path:
        return _read_file_review_target(command.file_path)

    if command.pr_number is not None:
        if not command.repo:
            msg = "repo is required when resolving hostile reviewer pr_number input"
            raise ValueError(msg)
        return _read_pr_diff(repo=command.repo, pr_number=command.pr_number)

    msg = "hostile reviewer start command requires file_path or pr_number"
    raise ValueError(msg)


def _read_file_review_target(file_path: str) -> str:
    path = Path(file_path).expanduser()
    if not path.exists():
        msg = f"hostile reviewer file_path does not exist: {file_path}"
        raise FileNotFoundError(msg)
    if not path.is_file():
        msg = f"hostile reviewer file_path is not a file: {file_path}"
        raise IsADirectoryError(msg)

    content = path.read_text(encoding="utf-8")
    return f"File review target: {file_path}\n\n{content}"


def _read_pr_diff(repo: str, pr_number: int) -> str:
    try:
        result = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "--repo", repo, "--color=never"],
            capture_output=True,
            check=False,
            text=True,
            timeout=GH_PR_DIFF_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        msg = "gh CLI is required to resolve hostile reviewer pr_number input"
        raise RuntimeError(msg) from exc
    except subprocess.TimeoutExpired as exc:
        msg = f"timed out resolving hostile reviewer PR diff for {repo}#{pr_number}"
        raise RuntimeError(msg) from exc

    if result.returncode != 0:
        stderr = result.stderr.strip()
        msg = f"failed to resolve hostile reviewer PR diff for {repo}#{pr_number}"
        if stderr:
            msg = f"{msg}: {stderr}"
        raise RuntimeError(msg)

    diff = result.stdout
    if not diff.strip():
        msg = f"resolved empty hostile reviewer PR diff for {repo}#{pr_number}"
        raise ValueError(msg)
    return diff


def _parse_handler_payload(payload: Mapping[str, object]) -> ModelWorkflowInput:
    if "diff_content" in payload or "model_keys" in payload:
        return ModelWorkflowInput(**payload)

    command = ModelHostileReviewerStartCommand(**payload)
    return workflow_input_from_start_command(command)


def _coerce_handler_payload(
    input_data: Mapping[str, object] | BaseModel | None,
    kwargs: Mapping[str, object],
) -> dict[str, object]:
    if input_data is not None and kwargs:
        msg = "pass either input_data or keyword payload fields, not both"
        raise TypeError(msg)
    if input_data is None:
        payload = dict(kwargs)
    elif isinstance(input_data, BaseModel):
        payload = input_data.model_dump()
    elif isinstance(input_data, Mapping):
        payload = dict(input_data)
    else:
        msg = (
            f"unsupported hostile reviewer handler payload: {type(input_data).__name__}"
        )
        raise TypeError(msg)

    for key in RUNTIME_LOCAL_METADATA_KEYS:
        payload.pop(key, None)
    return payload


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

    async def handle(
        self,
        input_data: Mapping[str, object] | BaseModel | None = None,
        **kwargs: Any,
    ) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Accepts either the contract event model (ModelHostileReviewerStartCommand)
        or the internal ModelWorkflowInput shape. RuntimeLocal's bus adapter
        invokes handlers with keyword payload fields, while direct unit callers
        commonly pass a dict.
        """
        payload = _coerce_handler_payload(input_data, kwargs)
        parsed = await asyncio.to_thread(_parse_handler_payload, payload)
        if self._adapter is None:
            msg = "inference_adapter not set — call set_adapter() first"
            raise RuntimeError(msg)
        result = await run_hostile_review_workflow(parsed, self._adapter)
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
    "workflow_input_from_start_command",
]
