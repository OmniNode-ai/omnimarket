"""HandlerDesignToPlan — FSM state machine for the design-to-plan workflow."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime

from omnimarket.inference.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceAdapter,
)
from omnimarket.inference.bridge_config_loader import (
    load_inference_bridge_config_from_env_async,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_command import (
    ModelDesignToPlanCommand,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_result import (
    ModelDesignToPlanResult,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_state import (
    TERMINAL_PHASES,
    EnumDesignToPlanPhase,
    ModelDesignToPlanCompletedEvent,
    ModelDesignToPlanPhaseEvent,
    ModelDesignToPlanState,
    next_phase,
)

logger = logging.getLogger(__name__)

_PLAN_TASK_HEADING = re.compile(r"^## (?:Task|Phase) \d+:.+$", re.MULTILINE)
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_CODE_FENCE = re.compile(r"^```[a-zA-Z]*\n(.*?)\n```\s*$", re.DOTALL)

_PLAN_SYSTEM_PROMPT = """You turn a design topic into an implementation plan.
Reply with the plan only, as markdown, in exactly this shape:

# <short plan title>

## Task 1: <task title>
<what to build, in one or two sentences>

## Task 2: <task title>
Dependencies: Task 1
<what to build, in one or two sentences>

Rules: three to six tasks; every task heading is "## Task N:" with N counting
up from 1; every task has a non-empty body; write "Dependencies: Task N" only
when a task needs an earlier one; no text outside the plan."""


def _extract_plan_markdown(raw: str) -> str:
    """Strip reasoning blocks and an enclosing code fence from a model answer."""
    text = _THINK_BLOCK.sub("", raw).strip()
    fenced = _CODE_FENCE.match(text)
    return fenced.group(1).strip() if fenced else text


def _plan_structure_error(plan: str) -> str | None:
    """Return why ``plan`` is not a parseable plan, or None when it is.

    Mirrors the section rule the plan-to-tickets parser applies (``## Task N:``
    or ``## Phase N:`` headings, each with a body) without importing that node.
    """
    headings = list(_PLAN_TASK_HEADING.finditer(plan))
    if not headings:
        return "no '## Task N:' sections found"
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(plan)
        if not plan[match.end() : end].strip():
            return f"section {match.group(0)!r} has no content"
    return None


class HandlerDesignToPlan:
    """FSM handler for the design-to-plan workflow.

    The brainstorm phase asks a model, reached through the shared inference
    bridge, to turn the command topic into a markdown plan; the structure phase
    checks that the answer parses as a plan. The plan rides on the completed
    event as ``plan_content``. A command that names an existing ``plan_path``
    skips the model. Inject ``inference_bridge`` in tests; otherwise the default
    bridge is built lazily on first ``handle`` from the lane's ``LLM_*_URL``
    configuration.
    """

    def __init__(
        self,
        inference_bridge: ModelInferenceAdapter | None = None,
        plan_model_key: str = "qwen3-coder",
        plan_timeout_seconds: float = 240.0,
    ) -> None:
        self._bridge: ModelInferenceAdapter | None = inference_bridge
        self._bridge_lock = asyncio.Lock()
        self._plan_model_key = plan_model_key
        self._plan_timeout_seconds = plan_timeout_seconds

    async def _ensure_bridge(self) -> ModelInferenceAdapter:
        """Return the inference bridge, building the default one once on demand."""
        if self._bridge is not None:
            return self._bridge
        async with self._bridge_lock:
            if self._bridge is None:
                self._bridge = AdapterInferenceBridge(
                    await load_inference_bridge_config_from_env_async()
                )
            return self._bridge

    def start(
        self, command: ModelDesignToPlanCommand, plan_content: str | None = None
    ) -> ModelDesignToPlanState:
        """Initialize FSM state from a start command."""
        return ModelDesignToPlanState(
            correlation_id=command.correlation_id,
            current_phase=EnumDesignToPlanPhase.IDLE,
            topic=command.topic,
            plan_path=str(command.plan_path) if command.plan_path else None,
            plan_content=plan_content,
            dry_run=command.dry_run,
            no_launch=command.no_launch,
            plan_only=command.plan_only,
        )

    def advance(
        self,
        state: ModelDesignToPlanState,
        phase_success: bool,
        error_message: str | None = None,
        plan_path: str | None = None,
        review_rounds: int = 0,
    ) -> tuple[ModelDesignToPlanState, ModelDesignToPlanPhaseEvent]:
        """Advance the FSM by one phase.

        On success: transitions to the next phase.
        On failure: increments consecutive_failures. At threshold -> FAILED.
        """
        from_phase = state.current_phase

        if from_phase in TERMINAL_PHASES:
            msg = f"Cannot advance from terminal phase: {from_phase}"
            raise ValueError(msg)

        if not phase_success:
            new_failures = state.consecutive_failures + 1
            if new_failures >= state.max_consecutive_failures:
                to_phase = EnumDesignToPlanPhase.FAILED
                err = (
                    error_message
                    or f"Circuit breaker: {new_failures} consecutive failures"
                )
                new_state = state.model_copy(
                    update={
                        "current_phase": to_phase,
                        "consecutive_failures": new_failures,
                        "error_message": err,
                    }
                )
            else:
                to_phase = from_phase
                new_state = state.model_copy(
                    update={
                        "consecutive_failures": new_failures,
                        "error_message": error_message,
                    }
                )
            event = ModelDesignToPlanPhaseEvent(
                correlation_id=state.correlation_id,
                from_phase=from_phase,
                to_phase=to_phase,
                success=False,
                error_message=error_message,
            )
            return new_state, event

        to_phase = next_phase(from_phase)
        updates: dict[str, object] = {
            "current_phase": to_phase,
            "consecutive_failures": 0,
            "error_message": None,
            "review_rounds": state.review_rounds + review_rounds,
        }
        if plan_path is not None:
            updates["plan_path"] = plan_path

        new_state = state.model_copy(update=updates)
        event = ModelDesignToPlanPhaseEvent(
            correlation_id=state.correlation_id,
            from_phase=from_phase,
            to_phase=to_phase,
            success=True,
        )
        return new_state, event

    def make_completed_event(
        self,
        state: ModelDesignToPlanState,
    ) -> ModelDesignToPlanCompletedEvent:
        """Create a completion event from the final state."""
        return ModelDesignToPlanCompletedEvent(
            correlation_id=state.correlation_id,
            final_phase=state.current_phase,
            plan_path=state.plan_path,
            plan_content=state.plan_content,
            error_message=state.error_message,
        )

    def serialize_event(self, event: ModelDesignToPlanPhaseEvent) -> bytes:
        """Serialize a phase event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def run_full_pipeline(
        self,
        command: ModelDesignToPlanCommand,
        phase_results: dict[EnumDesignToPlanPhase, bool] | None = None,
        *,
        plan_content: str | None = None,
        phase_errors: dict[EnumDesignToPlanPhase, str] | None = None,
    ) -> tuple[
        ModelDesignToPlanState,
        list[ModelDesignToPlanPhaseEvent],
        ModelDesignToPlanCompletedEvent,
    ]:
        """Run a complete pipeline through all phases.

        phase_results maps each phase to success/failure. If not provided,
        all phases succeed. phase_errors supplies the message for a failed
        phase; ``plan_content`` is the plan the brainstorm phase produced.
        """
        state = self.start(command, plan_content=plan_content)
        events: list[ModelDesignToPlanPhaseEvent] = []
        results = phase_results or {}
        errors = phase_errors or {}

        while state.current_phase not in TERMINAL_PHASES:
            target = next_phase(state.current_phase)
            success = results.get(target, True)
            error_msg = (
                None if success else errors.get(target, f"Phase {target.value} failed")
            )

            state, event = self.advance(
                state,
                phase_success=success,
                error_message=error_msg,
            )
            events.append(event)

            if not success and state.current_phase not in TERMINAL_PHASES:
                break

        completed = self.make_completed_event(state)
        return state, events, completed

    async def _brainstorm(
        self, command: ModelDesignToPlanCommand
    ) -> tuple[
        str | None,
        dict[EnumDesignToPlanPhase, bool],
        dict[EnumDesignToPlanPhase, str],
    ]:
        """Ask the model for a plan; return it with per-phase outcomes.

        Brainstorm fails when the model call fails or answers nothing; structure
        fails when the answer is not a parseable plan. A command with an existing
        ``plan_path`` skips the model.
        """
        if command.plan_path:
            return None, {}, {}
        brainstorm = EnumDesignToPlanPhase.BRAINSTORM
        structure = EnumDesignToPlanPhase.STRUCTURE
        try:
            bridge = await self._ensure_bridge()
            raw = await bridge.infer(
                model_key=self._plan_model_key,
                system_prompt=_PLAN_SYSTEM_PROMPT,
                user_prompt=f"Design topic:\n{command.topic}",
                timeout_seconds=self._plan_timeout_seconds,
                temperature=0.2,
            )
        except Exception as exc:
            logger.warning(
                "design-to-plan model call failed (correlation_id=%s): %s",
                command.correlation_id,
                exc,
            )
            return (
                None,
                {brainstorm: False},
                {brainstorm: f"Phase brainstorm failed: {exc}"},
            )
        plan = _extract_plan_markdown(raw)
        if not plan:
            return (
                None,
                {brainstorm: False},
                {brainstorm: "Phase brainstorm failed: empty model answer"},
            )
        problem = _plan_structure_error(plan)
        if problem is not None:
            return (
                None,
                {structure: False},
                {structure: f"Phase structure failed: {problem}"},
            )
        return plan, {}, {}

    async def handle(
        self,
        command: ModelDesignToPlanCommand,
        *,
        phase_results: dict[EnumDesignToPlanPhase, bool] | None = None,
    ) -> ModelDesignToPlanResult:
        """Primary entry point — brainstorms a plan, runs the FSM, returns a receipt.

        Wraps the pipeline products in a typed ``ModelDesignToPlanResult``. The
        runtime bus adapter (``LocalRuntimeBusAdapter``) only accepts a
        ``BaseModel``/``dict``/``None`` handler return; a raw ``tuple`` return
        crashes with ``ONEX_CORE_095_HANDLER_EXECUTION_ERROR`` (OMN-13841).
        Only the completed event is returned: this handler never builds the
        plan-to-tickets command, so with the launch off (or on) nothing is
        published to the plan-to-tickets-start topic from here.
        """
        plan, brainstormed, errors = await self._brainstorm(command)
        results = {**brainstormed, **(phase_results or {})}
        state, events, completed = self.run_full_pipeline(
            command, phase_results=results, plan_content=plan, phase_errors=errors
        )
        return ModelDesignToPlanResult(
            final_state=state,
            phase_events=events,
            completed_event=completed,
        )

    def _started_at(self) -> datetime:
        return datetime.now(tz=UTC)


__all__: list[str] = ["HandlerDesignToPlan", "ModelDesignToPlanResult"]
