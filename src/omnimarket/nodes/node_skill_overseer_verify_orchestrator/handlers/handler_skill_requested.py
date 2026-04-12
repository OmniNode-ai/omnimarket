# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill dispatch handler for node_skill_overseer_verify_orchestrator.

Dispatches overseer_verify skill invocations to the polymorphic agent (Polly)
via the injected task_dispatcher, then parses the structured RESULT: block.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from ..models.model_skill_request import ModelSkillRequest
from ..models.model_skill_result import ModelSkillResult, SkillResultStatus

__all__ = ["handle_skill_requested"]

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


async def handle_skill_requested(
    request: ModelSkillRequest,
    *,
    task_dispatcher: TaskDispatcher,
) -> ModelSkillResult:
    """Dispatch an overseer_verify skill request to Polly and return a structured result."""
    args_str = _build_args_string(request.args)
    args_clause = f" with args: {args_str}" if args_str else ""

    prompt = (
        f"Execute the skill defined at {request.skill_path!r}{args_clause}.\n"
        f"Read the skill definition from that path before executing.\n"
        f"After execution, you MUST include a structured RESULT: block in your "
        f"output with the following format:\n\n"
        f"RESULT:\n"
        f"status: <success|failed|partial>\n"
        f"error: <error detail or leave blank>\n"
    )

    logger.debug(
        "Dispatching overseer_verify skill %r to Polly (skill_path=%r)",
        request.skill_name,
        request.skill_path,
    )

    try:
        raw_output: str = await task_dispatcher(prompt)
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
