# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The independent judge: which model judges, what it is asked, what it said (OMN-20489).

The model families come from the independent-judge configuration; the
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
_VERDICT_LINE = re.compile(r"VERDICT\s*:\s*\**\s*([A-Za-z]+)", re.IGNORECASE)


@lru_cache(maxsize=1)
def answer_gate_contract() -> dict[str, Any]:
    """This node's contract, read once."""
    text = files(_NODE_PACKAGE).joinpath("contract.yaml").read_text(encoding="utf-8")
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError(f"{_NODE_PACKAGE} contract.yaml is not a mapping")
    return loaded


@lru_cache(maxsize=1)
def independent_judge_config() -> dict[str, Any]:
    """The independent-judge rollout flag and per-class judge prompts (OMN-20910)."""
    text = (
        files("omnimarket")
        .joinpath("configs", "delegation_independent_judge.v1.yaml")
        .read_text(encoding="utf-8")
    )
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError("delegation_independent_judge.v1.yaml is not a mapping")
    return loaded


def model_family(model_id: str) -> str | None:
    """The declared family of ``model_id``, or ``None`` when no family matches."""
    lowered = model_id.lower()
    for entry in independent_judge_config()["model_families"]:
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
    producer's. A backend reserved for explicit pins qualifies only when no
    ordinary backend does (OMN-20910: ordinary judging must not spend a
    provider the contract reserves for operator tests). ``None`` when the
    producer's family is unknown or no backend qualifies; the gate then
    refuses the text answer.
    """
    producer_family = model_family(producer_model_id)
    if producer_family is None:
        return None
    capability = answer_gate_contract()["judge_capability"]
    pinned: Mapping[str, Any] | None = None
    for backend in backends:
        if capability not in (backend.get("capabilities") or ()):
            continue
        judge_family = model_family(str(backend.get("model_name") or ""))
        if judge_family is None or judge_family == producer_family:
            continue
        if not backend.get("explicit_pin_only"):
            return backend
        if pinned is None:
            pinned = backend
    return pinned


def independent_judge_armed(task_type: str) -> bool:
    """Whether production calls the routing contract's judge for ``task_type``.

    Read from the ``independent_judge.armed_task_classes`` flag in
    ``configs/delegation_independent_judge.v1.yaml`` (OMN-20910); a class not
    listed keeps its contract verdict unless the caller supplied a judge itself.
    """
    armed = (
        independent_judge_config()
        .get("independent_judge", {})
        .get("armed_task_classes")
    )
    return task_type in (armed or ())


def _class_prompt(task_type: str | None) -> Mapping[str, Any] | None:
    if task_type is None:
        return None
    by_class = independent_judge_config().get("judge_prompt_by_task_class") or {}
    declared = by_class.get(task_type)
    return declared if isinstance(declared, Mapping) else None


def render_judge_prompt(
    *, instruction: str, source: str, answer: str, task_type: str | None = None
) -> tuple[str, str]:
    """The judge's (system, user) messages for one answer.

    An empty ``instruction`` drops the INSTRUCTION block: a delegated prompt
    states its instruction inside the source, and repeating it costs tokens.
    A task class that declares its own judge prompt (OMN-20910) uses it.
    """
    prompt = _class_prompt(task_type) or answer_gate_contract()["judge_prompt"]
    template = str(prompt["user"])
    if not instruction.strip():
        template = template.replace("INSTRUCTION:\n{instruction}\n\n", "")
    # One pass, so a brace token inside the source or answer is left as text.
    values = {"instruction": instruction, "source": source, "answer": answer}
    user = _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)
    return str(prompt["system"]), user


def parse_judge_reply(
    reply: str, *, task_type: str | None = None
) -> tuple[EnumIndependentJudgeVerdict, str]:
    """Read the judge's verdict; anything else is UNPARSEABLE, never a pass."""
    declared = _class_prompt(task_type)
    if declared is not None and declared.get("reply") == "verdict_line":
        return _parse_verdict_line(reply)
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


def _parse_verdict_line(reply: str) -> tuple[EnumIndependentJudgeVerdict, str]:
    """The LAST ``VERDICT: ACCEPT|REJECT`` line decides (OMN-20910).

    The judge reasons first and may quote the rubric's own verdict wording, so
    only the final verdict line counts; a final line naming neither word is
    UNPARSEABLE rather than read from an earlier one.
    """
    matches = list(_VERDICT_LINE.finditer(reply or ""))
    if not matches:
        return EnumIndependentJudgeVerdict.UNPARSEABLE, "no VERDICT line in reply"
    last = matches[-1]
    word = last.group(1).upper()
    if word not in ("ACCEPT", "REJECT"):
        return EnumIndependentJudgeVerdict.UNPARSEABLE, f"unknown verdict {word!r}"
    reasoning = reply[: last.start()].strip()
    reason = reasoning[-300:] if reasoning else ""
    verdict = (
        EnumIndependentJudgeVerdict.ACCEPT
        if word == "ACCEPT"
        else EnumIndependentJudgeVerdict.REJECT
    )
    return verdict, reason


__all__ = [
    "answer_gate_contract",
    "independent_judge_armed",
    "independent_judge_config",
    "model_family",
    "parse_judge_reply",
    "render_judge_prompt",
    "select_independent_judge",
]
