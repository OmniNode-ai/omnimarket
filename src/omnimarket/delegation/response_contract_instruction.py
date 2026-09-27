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
    "compose_system_prompt_with_response_contract_instruction",
    "render_extraction_marker_instruction",
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


def _declared_property_names(response_contract: dict[str, object]) -> list[str]:
    """Return object-property names in declaration order."""
    properties = response_contract.get("properties")
    if not isinstance(properties, dict):
        return []
    return [name for name in properties if isinstance(name, str)]


def render_response_contract_instruction(
    response_contract: dict[str, object] | None,
    *,
    output_shape: str | None = None,
    render_start_marker: str | None = None,
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
    declared_output_shape = output_shape or (
        response_contract.get("x-omninode-output-shape")
        if response_contract is not None
        else None
    )
    if declared_output_shape in {"markdown", "plain_text"} and not render_start_marker:
        raise ValueError("text output shapes require declared extraction markers")
    # OMN-19406: the text shapes used to say "do not include analysis or
    # reasoning before the deliverable". That sentence exists to keep a leaked
    # scratchpad out of the answer (OMN-18379), but read literally it forbids
    # the reasoning that blocking rules such as methodical_analysis and
    # step_by_step_explanation ask for, and the model obeyed it. It now bans
    # only what it was for -- scratch work ahead of the marker -- and places
    # any explanation the request asks for inside the deliverable.
    if declared_output_shape == "markdown":
        marker_instruction = render_extraction_marker_instruction(render_start_marker)
        return (
            "Respond with only the requested Markdown deliverable. Do not write "
            "scratch work, notes to yourself or a thinking process before the "
            "extraction marker. Any explanation, steps or reasoning the request "
            "asks for belongs inside the deliverable, after the marker. "
            f"{marker_instruction}"
        )
    if declared_output_shape == "plain_text":
        marker_instruction = render_extraction_marker_instruction(render_start_marker)
        return (
            "Respond with only the requested plain-text deliverable. Do not write "
            "scratch work, notes to yourself, a thinking process or Markdown "
            "fencing before the extraction marker. Any explanation, steps or "
            "reasoning the request asks for belongs inside the deliverable, "
            "after the marker. "
            f"{marker_instruction}"
        )
    if response_contract is None:
        raise ValueError("json output shape requires a response contract")
    schema_text = json.dumps(response_contract, indent=2, sort_keys=True)
    value_type = response_contract.get("type")
    if value_type == "object":
        output_shape = "a single JSON object"
    elif value_type == "array":
        output_shape = "a single JSON array"
    else:
        output_shape = "a single JSON value"
    lines = [
        f"You must respond with {output_shape} that validates against this JSON Schema:",
        "",
        schema_text,
        "",
    ]
    required = _required_key_names(response_contract)
    if required:
        lines.append(
            "The response object must contain these required keys, spelled "
            "exactly this way: " + ", ".join(required) + "."
        )
    properties = _declared_property_names(response_contract)
    optional = [name for name in properties if name not in required]
    if optional:
        lines.append(
            "These declared keys are optional and may be omitted: "
            + ", ".join(optional)
            + "."
        )
    if response_contract.get("additionalProperties") is False:
        lines.append("Do not include keys other than the declared properties.")
    elif response_contract.get("additionalProperties") is True:
        lines.append("Additional properties are permitted by this schema.")
    lines.append(
        "Respond with only that JSON object. Do not write any reasoning, "
        "explanation or prose before or after it, and do not wrap it in a "
        "markdown code fence (```)."
    )
    return "\n".join(lines)


def render_extraction_marker_instruction(render_start_marker: str | None) -> str:
    """Describe the one exact authority-owned boundary for a text deliverable.

    Public since OMN-18349: the delegation paths restate this sentence, and only
    this sentence, as the first line of the user turn. Restating the whole
    Markdown instruction there turned a vague code request into a Markdown
    bullet list (1 of 4 local code answers compiled, against 4 of 4 without it).
    """
    if render_start_marker is None:
        raise ValueError("text output shapes require a render start marker")
    return (
        "Put this exact extraction start marker on its own line immediately "
        f"before the deliverable: {render_start_marker}"
    )


def compose_system_prompt_with_response_contract(
    *,
    system_prompt: str,
    response_contract: dict[str, object] | None,
    output_shape: str | None = None,
    render_start_marker: str | None = None,
) -> str:
    """Append the rendered contract instruction to a system prompt.

    A missing JSON schema leaves the prompt unchanged only when no resolved
    text output shape was supplied. A task-class markdown or plain-text
    contract has no JSON schema by design, but still needs its exact extraction
    marker conveyed to the model.

    The instruction is APPENDED rather than prepended so a caller-supplied
    system prompt keeps its leading position, and so the output-shape directive
    -- the thing the model must still be obeying when it stops generating --
    sits closest to the generation boundary.
    """
    instruction = (
        render_response_contract_instruction(
            response_contract,
            output_shape=output_shape,
            render_start_marker=render_start_marker,
        )
        if response_contract is not None or output_shape in {"markdown", "plain_text"}
        else None
    )
    return compose_system_prompt_with_response_contract_instruction(
        system_prompt=system_prompt,
        instruction=instruction,
    )


def compose_system_prompt_with_response_contract_instruction(
    *,
    system_prompt: str,
    instruction: str | None,
) -> str:
    """Append an already-resolved response-contract instruction unchanged."""
    if instruction is None:
        return system_prompt
    if not system_prompt:
        return instruction
    return f"{system_prompt}\n\n{instruction}"
