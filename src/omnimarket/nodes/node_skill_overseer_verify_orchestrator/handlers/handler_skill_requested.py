# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill dispatch handler for node_skill_overseer_verify_orchestrator.

Dispatches overseer_verify skill invocations to the polymorphic agent (Polly)
via a task dispatcher, then parses the structured RESULT: block.

Container-driven pattern (OMN-13603): the handler is a class whose constructor
takes the injectable ``container`` so the runtime resolver constructs it at boot
via known-param injection — replacing the previous function-form handler
``handle_skill_requested(request, *, task_dispatcher)`` whose required params the
resolver could not satisfy (it quarantined at boot). The polymorphic-agent
``TaskDispatcher`` is resolved at the dispatch boundary inside ``handle()``. Until
a real bus-backed dispatcher is registered, ``handle()`` returns a structured
FAILED ``ModelSkillResult`` rather than crashing — mirroring the sibling
``node_skill_dispatch_engine_orchestrator`` scaffold semantics.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..models.model_skill_request import ModelSkillRequest
from ..models.model_skill_result import ModelSkillResult, SkillResultStatus

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer

__all__ = ["HandlerSkillRequested", "TaskDispatcher"]

logger = logging.getLogger(__name__)

TaskDispatcher = Callable[[str], Awaitable[str]]

_RESULT_BLOCK_MARKER = "RESULT:"
_STATUS_KEY = "status:"
_ERROR_KEY = "error:"


def _build_args_string(args: dict[str, str]) -> str:
    if not args:
        return ""
    parts: list[str] = []
    for key, value in args.items():
        if value == "" or value == "true":
            parts.append(f"--{key}")
        else:
            parts.append(f"--{key} {value}")
    return " ".join(parts)


def _parse_result_block(output: str) -> tuple[SkillResultStatus, str | None]:
    marker_idx = output.find(_RESULT_BLOCK_MARKER)
    if marker_idx == -1:
        logger.warning("No RESULT: block found in output; returning PARTIAL")
        return SkillResultStatus.PARTIAL, "No RESULT: block in output"

    block_text = output[marker_idx + len(_RESULT_BLOCK_MARKER) :]
    block_lines: list[str] = []
    for line in block_text.splitlines():
        if block_lines and line.strip() == "":
            break
        block_lines.append(line)

    status: SkillResultStatus = SkillResultStatus.PARTIAL
    error: str | None = None

    for line in block_lines:
        stripped = line.strip().lower()
        if stripped.startswith(_STATUS_KEY):
            raw_status = line.strip()[len(_STATUS_KEY) :].strip().lower()
            if raw_status == "success":
                status = SkillResultStatus.SUCCESS
            elif raw_status == "failed":
                status = SkillResultStatus.FAILED
            else:
                status = SkillResultStatus.PARTIAL
        elif stripped.startswith(_ERROR_KEY):
            raw_error = line.strip()[len(_ERROR_KEY) :].strip()
            error = raw_error if raw_error else None

    return status, error


def _build_dispatch_prompt(request: ModelSkillRequest) -> str:
    args_str = _build_args_string(request.args)
    args_clause = f" with args: {args_str}" if args_str else ""
    return (
        f"Execute the skill defined at {request.skill_path!r}{args_clause}.\n"
        f"Read the skill definition from that path before executing.\n"
        f"After execution, you MUST include a structured RESULT: block in your "
        f"output with the following format:\n\n"
        f"RESULT:\n"
        f"status: <success|failed|partial>\n"
        f"error: <error detail or leave blank>\n"
    )


class HandlerSkillRequested:
    """Dispatches the overseer_verify skill to the polymorphic agent.

    The task dispatcher is resolved at the dispatch boundary, not at
    construction — so the boot resolver can build this handler from the
    injectable ``container`` alone. A real bus-backed dispatcher may be provided
    explicitly (tests / future wiring); when none is available the handler
    returns a structured FAILED result instead of crashing.
    """

    def __init__(
        self,
        container: ModelONEXContainer,
        *,
        task_dispatcher: TaskDispatcher | None = None,
    ) -> None:
        self._container = container
        self._task_dispatcher = task_dispatcher

    def _resolve_dispatcher(self) -> TaskDispatcher | None:
        """Return the task dispatcher, if one is available.

        Prefers an explicitly-provided dispatcher (tests / future wiring). No
        bus-backed dispatcher provider is registered yet, so this returns None
        otherwise — handled as a structured FAILED result by ``handle()``.
        """
        return self._task_dispatcher

    async def handle(self, request: ModelSkillRequest) -> ModelSkillResult:
        """Dispatch an overseer_verify skill request and return a structured result."""
        dispatcher = self._resolve_dispatcher()
        if dispatcher is None:
            logger.warning(
                "No task dispatcher available for skill %r; returning FAILED",
                request.skill_name,
            )
            return ModelSkillResult(
                skill_name=request.skill_name,
                status=SkillResultStatus.FAILED,
                error="no task dispatcher available (overseer_verify not yet wired)",
            )

        prompt = _build_dispatch_prompt(request)
        logger.debug(
            "Dispatching overseer_verify skill %r to Polly (skill_path=%r)",
            request.skill_name,
            request.skill_path,
        )

        try:
            raw_output: str = await dispatcher(prompt)
        except Exception:
            logger.exception(
                "task_dispatcher raised for skill %r",
                request.skill_name,
            )
            return ModelSkillResult(
                skill_name=request.skill_name,
                status=SkillResultStatus.FAILED,
                error="task_dispatcher raised an exception",
            )

        output_str: str = str(raw_output) if raw_output is not None else ""
        status, error = _parse_result_block(output_str)

        logger.debug(
            "Skill %r completed with status=%s",
            request.skill_name,
            status,
        )

        return ModelSkillResult(
            skill_name=request.skill_name,
            status=status,
            output=output_str,
            error=error,
        )
