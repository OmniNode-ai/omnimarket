# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render a declared response contract as an instruction the model can read.

OMN-7942. A caller that declares a ``response_contract`` was having its answer
graded against a JSON Schema the model was never shown. The contract flowed
from the CLI through ``ModelDelegateSkillRequest`` and the dispatch port into
the quality gate, and stopped there -- ``handler_llm_delegation_call`` carried
no reference to it, so no part of the outbound chat-completions payload ever
mentioned it.

Measured on the live ``.201`` lab endpoint 2026-09-18, correlation
``4c053fe9-1fce-4204-8705-2ed009fdc32d``: the served model's own recorded
reasoning read "We have no explicit schema.", it then guessed a key name the
contract does not contain, three local attempts failed the deterministic floor,
and the router climbed to ``cheap_cloud`` and spent $0.003856 before the run
terminated failed. The same gate had already been failed in one run by two
other models on two other providers, which is what rules out reading this as a
property of any one model.

This module is the rendering half only. It is a pure function over the declared
schema, with no I/O and no knowledge of any backend, so the same text is
produced for a local vLLM rung and a metered cloud rung alike. Deliberately NOT
here: the provider-native structured-output path (vLLM ``guided_json``,
``response_format: json_schema``). No backend binding declares that capability
today, and asserting it against a backend that does not support it converts a
gradeable near-miss into an HTTP 400. That half remains OMN-7942's other side
and needs a capability field on the binding contract first.

The rendering is deterministic under key insertion order -- two dicts that
differ only in ordering render byte-identically -- so the instruction a given
declared contract produces cannot vary between runs.
"""

from __future__ import annotations

import json

__all__ = [
    "compose_system_prompt_with_response_contract",
    "render_response_contract_instruction",
]


def _required_key_names(response_contract: dict[str, object]) -> list[str]:
    """Return the schema's declared required keys, in declaration order.

    A schema whose ``required`` is absent or is not a list of strings yields an
    empty list: the schema body is still rendered in full, so the instruction
    degrades to "here is the schema" rather than refusing. Naming the keys is
    an emphasis on top of the schema, never the only place they appear.
    """
    required = response_contract.get("required")
    if not isinstance(required, list):
        return []
    return [item for item in required if isinstance(item, str)]


def render_response_contract_instruction(
    response_contract: dict[str, object],
) -> str:
    """Render a JSON Schema as a model-readable output instruction.

    The required key names are called out ABOVE the schema body as well as
    appearing inside it. That redundancy is deliberate and is aimed at the
    measured failure: the model did not misread the schema, it never saw one,
    and what it then guessed wrong was a key NAME.

    The prohibition on prose and on a markdown fence is part of the same
    instruction rather than a separate concern. Telling a model to emit JSON
    without telling it not to wrap the JSON trades a missing-schema failure for
    a fenced-output failure.
    """
    schema_text = json.dumps(response_contract, indent=2, sort_keys=True)
    lines = [
        "You must respond with a single JSON object that validates against "
        "this JSON Schema:",
        "",
        schema_text,
        "",
    ]
    required = _required_key_names(response_contract)
    if required:
        lines.append(
            "The response object must contain exactly these keys, spelled "
            "exactly this way: " + ", ".join(required) + "."
        )
    lines.append(
        "Respond with only that JSON object. Do not write any reasoning, "
        "explanation or prose before or after it, and do not wrap it in a "
        "markdown code fence (```)."
    )
    return "\n".join(lines)


def compose_system_prompt_with_response_contract(
    *,
    system_prompt: str,
    response_contract: dict[str, object] | None,
) -> str:
    """Append the rendered contract instruction to a system prompt.

    ``None`` returns ``system_prompt`` unchanged, byte for byte, which is what
    keeps every caller that declares no contract sending exactly the prompt it
    sent before this change.

    The instruction is APPENDED rather than prepended so a caller-supplied
    system prompt keeps its leading position, and so the output-shape directive
    -- the thing the model must still be obeying when it stops generating --
    sits closest to the generation boundary.
    """
    if response_contract is None:
        return system_prompt
    instruction = render_response_contract_instruction(response_contract)
    if not system_prompt:
        return instruction
    return f"{system_prompt}\n\n{instruction}"
