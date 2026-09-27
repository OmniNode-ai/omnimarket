# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The response contract and prompt instruction for declared output files (OMN-19600).

A delegation that declares output files asks the model for ONE JSON object,
``{"files": [{"path": ..., "content": ...}, ...]}``, naming exactly the
declared paths. The JSON Schema built here is passed as the request's existing
``response_contract``, so the quality gate already enforces the shape before
anything is accepted (``_evaluate_response_contract``): no new gate, and no
wire change to the request.

The schema bounds what JSON Schema can express: the path must be one of the
declared paths, each content string is capped, and the array has exactly as
many items as there are declared files. "Each declared path exactly once" is
not expressible for objects, so the extract compute enforces it and records a
DUPLICATE or MISSING refusal.
"""

from __future__ import annotations

import json

from omnimarket.models.delegation.wire.model_delegation_output_files import (
    ModelDeclaredOutputs,
)


def build_declared_outputs_response_contract(
    declared: ModelDeclaredOutputs,
) -> dict[str, object]:
    """Return the JSON Schema a reply for ``declared`` must satisfy."""
    count = len(declared.files)
    largest = max(item.max_bytes for item in declared.files)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["files"],
        "properties": {
            "files": {
                "type": "array",
                "minItems": count,
                "maxItems": count,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "content"],
                    "properties": {
                        "path": {
                            "type": "string",
                            "enum": [item.path for item in declared.files],
                        },
                        "content": {"type": "string", "maxLength": largest},
                    },
                },
            }
        },
    }


def render_declared_outputs_instruction(declared: ModelDeclaredOutputs) -> str:
    """Return the sentence block that tells the model how to answer."""
    listing = "\n".join(
        f"- {item.path} ({item.kind.value}, at most {item.max_bytes} bytes)"
        for item in declared.files
    )
    example = json.dumps(
        {"files": [{"path": item.path, "content": "..."} for item in declared.files]}
    )
    return (
        "Answer with exactly one JSON object and nothing else, of the form "
        f"{example}. It must contain one entry for each of these files, with "
        "the complete file content as a string:\n"
        f"{listing}\n"
        "Use exactly these paths. Do not add other keys, files, prose or "
        "markdown fences."
    )


__all__ = [
    "build_declared_outputs_response_contract",
    "render_declared_outputs_instruction",
]
