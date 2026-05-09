"""Deterministic schema repair prompt builder — no LLM calls, same input = identical output."""

from __future__ import annotations

import json

from omnimarket.nodes.node_schema_repair_compute.models.model_repair_request import (
    ModelSchemaRepairRequest,
)
from omnimarket.nodes.node_schema_repair_compute.models.model_repair_result import (
    EnumRepairStatus,
    ModelSchemaRepairResult,
)

# Error types that cannot be fixed by re-prompting (structural/type constraints
# that the LLM fundamentally cannot satisfy given the schema).
_NON_REPAIRABLE_ERROR_TYPES: frozenset[str] = frozenset(
    {
        "missing",  # required field the LLM has no information to supply
        "extra_forbidden",  # LLM added fields the schema forbids — repairable
    }
)

# Extra-forbidden errors are actually repairable (LLM just needs to drop the field).
# Only "missing" with no default is truly non-repairable when the LLM lacks the data.
_TRULY_NON_REPAIRABLE: frozenset[str] = frozenset({"value_error.missing"})


def _is_repairable(validation_errors: list[dict[str, object]]) -> bool:
    """Return False only when errors indicate the LLM structurally cannot produce valid output."""
    for err in validation_errors:
        err_type = str(err.get("type", ""))
        # A missing required field with no way to derive a value cannot be repaired
        # by re-prompting unless the repair prompt explicitly instructs a default.
        if err_type == "missing":
            # Still repairable — the repair prompt will call it out explicitly.
            # We only mark non-repairable if there are zero fixable errors at all.
            pass
    # All known Pydantic v2 error types are potentially fixable by re-prompting
    # with explicit field-by-field instructions. We return True unconditionally
    # unless the error list is empty (nothing to repair).
    return len(validation_errors) > 0


def _format_error(error: dict[str, object]) -> str:
    loc = error.get("loc", ())
    if isinstance(loc, (list, tuple)):
        field_path = " -> ".join(str(part) for part in loc)
    else:
        field_path = str(loc)
    msg = str(error.get("msg", "unknown error"))
    err_type = str(error.get("type", "unknown"))
    return f"  - field '{field_path}': {msg} (type: {err_type})"


def _build_error_summary(validation_errors: list[dict[str, object]]) -> str:
    if not validation_errors:
        return "No validation errors reported."
    count = len(validation_errors)
    lines = [f"{count} validation error(s) found:"]
    for err in validation_errors:
        lines.append(_format_error(err))
    return "\n".join(lines)


def _escape_markdown_fence(value: str) -> str:
    return value.replace("```", "\\`\\`\\`")


def _build_repair_prompt(
    request: ModelSchemaRepairRequest,
    error_summary: str,
) -> str:
    schema_str = json.dumps(request.target_schema, indent=2)
    safe_malformed_output = _escape_markdown_fence(request.malformed_output)

    lines: list[str] = []
    lines.append("Your previous response did not conform to the required JSON schema.")
    lines.append("You must produce a corrected response that strictly validates.")
    lines.append("")
    lines.append("## Original Prompt")
    lines.append("")
    lines.append(request.original_prompt)
    lines.append("")
    lines.append("## Your Previous (Invalid) Response")
    lines.append("")
    lines.append("```")
    lines.append(safe_malformed_output)
    lines.append("```")
    lines.append("")
    lines.append("## What Went Wrong")
    lines.append("")
    lines.append(error_summary)
    lines.append("")
    lines.append("## Required Schema")
    lines.append("")
    lines.append("Your response MUST be valid JSON that matches this schema exactly:")
    lines.append("")
    lines.append("```json")
    lines.append(schema_str)
    lines.append("```")
    lines.append("")
    lines.append("## Instructions")
    lines.append("")
    lines.append(
        "1. Output ONLY valid JSON — no prose, no markdown fences, no explanation."
    )
    lines.append("2. Include every required field listed in the schema.")
    lines.append("3. Do not include fields not defined in the schema.")
    lines.append("4. Correct each field listed under 'What Went Wrong' above.")
    lines.append(
        "5. If a field value is unknown, use an appropriate empty/default value"
    )
    lines.append(
        "   (empty string for str, 0 for int/float, [] for arrays, {} for objects)."
    )
    lines.append("")
    lines.append("Respond with corrected JSON only:")

    return "\n".join(lines)


class HandlerSchemaRepair:
    """Build a targeted repair prompt from malformed LLM output + validation errors. Pure, deterministic, no I/O."""

    def handle(self, request: ModelSchemaRepairRequest) -> ModelSchemaRepairResult:
        error_summary = _build_error_summary(request.validation_errors)
        repairable = _is_repairable(request.validation_errors)
        repair_prompt = _build_repair_prompt(request, error_summary)

        return ModelSchemaRepairResult(
            status=EnumRepairStatus.OK,
            repair_prompt=repair_prompt,
            error_summary=error_summary,
            repairable=repairable,
            run_id=request.run_id,
        )


__all__ = ["HandlerSchemaRepair"]
