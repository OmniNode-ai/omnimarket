# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The independent judge: which model judges, what it is asked, what it said (OMN-20489).

Everything here is read from this node's contract.yaml: the model families, the
judge capability a routing-contract backend declares, and the judge prompt. No
judge backend or model is named in code; the caller passes the routing
contract's backends and this module picks the first one declaring the judge
capability whose model family differs from the producer's.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from functools import lru_cache
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.models.delegation.wire.model_answer_gate_request import (
    EnumIndependentJudgeVerdict,
)

_NODE_PACKAGE = "omnimarket.nodes.node_delegation_answer_gate_compute"
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_PLACEHOLDER = re.compile(r"\{(instruction|source|answer)\}")


@lru_cache(maxsize=1)
def answer_gate_contract() -> dict[str, Any]:
    """This node's contract, read once."""
    text = files(_NODE_PACKAGE).joinpath("contract.yaml").read_text(encoding="utf-8")
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError(f"{_NODE_PACKAGE} contract.yaml is not a mapping")
    return loaded


def model_family(model_id: str) -> str | None:
    """The declared family of ``model_id``, or ``None`` when no family matches."""
    lowered = model_id.lower()
    for entry in answer_gate_contract()["model_families"]:
        if any(token in lowered for token in entry["match"]):
            return str(entry["family"])
    return None


def select_independent_judge(
    producer_model_id: str, backends: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """The first judge backend, in routing-contract order, of another family.

    ``backends`` is the routing contract's backend list (contract plus overlay,
    resolved by the caller). A backend qualifies when it declares the judge
    capability and its model's family is known and differs from the
    producer's. ``None`` when the producer's family is unknown or no backend
    qualifies; the gate then refuses the text answer.
    """
    producer_family = model_family(producer_model_id)
    if producer_family is None:
        return None
    capability = answer_gate_contract()["judge_capability"]
    for backend in backends:
        if capability not in (backend.get("capabilities") or ()):
            continue
        judge_family = model_family(str(backend.get("model_name") or ""))
        if judge_family is not None and judge_family != producer_family:
            return backend
    return None


def render_judge_prompt(
    *, instruction: str, source: str, answer: str
) -> tuple[str, str]:
    """The judge's (system, user) messages for one answer.

    An empty ``instruction`` drops the INSTRUCTION block: a delegated prompt
    states its instruction inside the source, and repeating it costs tokens.
    """
    prompt = answer_gate_contract()["judge_prompt"]
    template = str(prompt["user"])
    if not instruction.strip():
        template = template.replace("INSTRUCTION:\n{instruction}\n\n", "")
    # One pass, so a brace token inside the source or answer is left as text.
    values = {"instruction": instruction, "source": source, "answer": answer}
    user = _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)
    return str(prompt["system"]), user


def parse_judge_reply(reply: str) -> tuple[EnumIndependentJudgeVerdict, str]:
    """Read the judge's JSON verdict; anything else is UNPARSEABLE, never a pass."""
    match = _JSON_OBJECT.search(reply or "")
    if match is None:
        return EnumIndependentJudgeVerdict.UNPARSEABLE, "no JSON object in reply"
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return EnumIndependentJudgeVerdict.UNPARSEABLE, "reply JSON does not parse"
    accept = parsed.get("accept") if isinstance(parsed, dict) else None
    if not isinstance(accept, bool):
        return EnumIndependentJudgeVerdict.UNPARSEABLE, "reply has no boolean accept"
    reason = str(parsed.get("reason") or "")
    verdict = (
        EnumIndependentJudgeVerdict.ACCEPT
        if accept
        else EnumIndependentJudgeVerdict.REJECT
    )
    return verdict, reason


__all__ = [
    "answer_gate_contract",
    "model_family",
    "parse_judge_reply",
    "render_judge_prompt",
    "select_independent_judge",
]
