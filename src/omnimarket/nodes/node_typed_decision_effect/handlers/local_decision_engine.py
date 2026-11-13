# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure mapping for a local vLLM decision engine (OMN-19432).

Maps a typed decision request to an OpenAI chat-completions body with guided
decoding, and maps the vLLM response back to the answer shape the Jev backend
returns and ``HandlerTypedDecision._parse_answer`` reads. No I/O, no endpoint,
no model id, no clock: the caller supplies the model id and reads the body.

Options are presented to the model as single-token codes, because labels that
share a first token would make the first-token probability ambiguous; each code
is one token, so the first-token logprobs are unambiguous per option.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any, Final

from omnimarket.nodes.node_typed_decision_effect.models.model_typed_decision import (
    EnumTypedDecisionKind,
    ModelTypedDecisionRequest,
)

__all__ = [
    "OPTION_CODES",
    "answer_payload",
    "build_request_body",
    "option_labels",
]

#: Twenty single-token codes, one per option, so the first-token logprobs are
#: unambiguous per option.
OPTION_CODES: Final[str] = "ABCDEFGHIJKLMNOPQRST"


def option_labels(request: ModelTypedDecisionRequest) -> list[str]:
    """The option labels in order, one per code, for the request's kind."""
    criteria = request.criteria
    if request.kind is EnumTypedDecisionKind.CHOICE and isinstance(criteria, dict):
        labels = list(criteria)
    elif request.kind is EnumTypedDecisionKind.SCORE and isinstance(criteria, list):
        labels = [str(i) for i in range(len(criteria))]
    else:
        labels = ["true", "false"]
    if len(labels) > len(OPTION_CODES):
        raise ValueError("too many options")
    return labels


def _prompt(request: ModelTypedDecisionRequest, labels: list[str]) -> str:
    """The user prompt: instructions, state, one line per option, then the rule."""
    instructions = request.instructions
    if not isinstance(instructions, str):
        instructions = json.dumps(instructions, sort_keys=True)
    state = json.dumps(request.state, sort_keys=True)
    criteria = request.criteria
    lines = [instructions, "", state, "", "Options:"]
    for code, label in zip(OPTION_CODES, labels, strict=False):
        description: object = None
        if isinstance(criteria, dict):
            description = criteria.get(label)
        elif isinstance(criteria, list):
            description = criteria[int(label)]
        line = f"{code}: {label}"
        if description is not None:
            line += f" - {description}"
        lines.append(line)
    lines.append("")
    lines.append("Answer with the code of exactly one option and nothing else.")
    return "\n".join(lines)


def build_request_body(
    request: ModelTypedDecisionRequest,
    *,
    model_id: str,
    max_tokens: int,
    request_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The JSON body for POST /v1/chat/completions with guided decoding.

    ``request_options`` are the site's extra top-level request fields from its
    overlay (for example a chat template switch); they never replace a field this
    function sets.
    """
    labels = option_labels(request)
    return {
        **(request_options or {}),
        "model": model_id,
        "max_tokens": max_tokens,
        "logprobs": True,
        "top_logprobs": len(labels),
        "structured_outputs": {"choice": list(OPTION_CODES[: len(labels)])},
        "messages": [{"role": "user", "content": _prompt(request, labels)}],
    }


def _probabilities(
    response_json: dict[str, Any],
    labels: list[str],
) -> list[float]:
    """The renormalised probability of each offered code at the answer token.

    The answer token is the last generated token that is an offered code: with a
    reasoning model the reply carries unconstrained reasoning tokens first and the
    guided answer after them, so the first token is not the answer's. Guided
    decoding masks the answer position to the offered codes; the masked entries are
    renormalised over the offered codes here, never trusted to sum to one.
    """
    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("response has no choices")
    logprobs = choices[0].get("logprobs")
    content = logprobs.get("content") if isinstance(logprobs, dict) else None
    if not isinstance(content, list) or not content:
        raise ValueError("response has no logprobs content")
    codes = OPTION_CODES[: len(labels)]
    answer = next(
        (entry for entry in reversed(content) if entry.get("token") in codes), None
    )
    if answer is None:
        raise ValueError("no generated token is an offered code")
    top = answer.get("top_logprobs")
    if not isinstance(top, list):
        raise ValueError("response has no top_logprobs")
    by_code = {entry.get("token"): entry.get("logprob") for entry in top}
    raw = [
        math.exp(by_code[code]) if by_code.get(code) is not None else 0.0
        for code in codes
    ]
    total = sum(raw)
    if total <= 0.0:
        raise ValueError("the model put no probability on any allowed option")
    return [value / total for value in raw]


def answer_payload(
    request: ModelTypedDecisionRequest,
    response_json: dict[str, Any],
) -> dict[str, Any]:
    """The answer dict in the same shape the Jev backend returns."""
    labels = option_labels(request)
    probs = _probabilities(response_json, labels)
    best = max(range(len(labels)), key=lambda i: (probs[i], -i))
    usage = response_json.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    payload: dict[str, Any] = {
        "model": response_json.get("model"),
        "usage": {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
        },
    }
    if request.kind is EnumTypedDecisionKind.CHOICE:
        payload["answers"] = {
            "decision": {
                "type": "choice",
                "choice": labels[best],
                "probabilities": dict(zip(labels, probs, strict=False)),
            }
        }
    elif request.kind is EnumTypedDecisionKind.SCORE:
        payload["answers"] = {
            "decision": {
                "type": "score",
                "probabilities": dict(zip(labels, probs, strict=False)),
                "score": sum(i * p for i, p in enumerate(probs)),
            }
        }
    else:
        payload["answers"] = {"decision": {"type": "noul", "noul": probs[0]}}
    return payload
