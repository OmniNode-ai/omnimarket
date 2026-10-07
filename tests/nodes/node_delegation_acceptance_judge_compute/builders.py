# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders shared by the acceptance judge tests."""

from __future__ import annotations

import json
from importlib.resources import files

from omnimarket.models.delegation_acceptance_judge.enum_acceptance_failure_class import (
    EnumAcceptanceFailureClass,
)
from omnimarket.models.delegation_acceptance_judge.enum_acceptance_operation import (
    EnumAcceptanceOperation,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_cell_event import (
    ModelAcceptanceCellEvent,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_item import (
    ModelAcceptanceItem,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_judge_request import (
    ModelAcceptanceJudgeRequest,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_verdict import (
    ModelAcceptanceVerdict,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.handler_delegation_acceptance_judge import (
    HandlerDelegationAcceptanceJudge,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.parse_rubric import (
    parse_rubric,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_judge_result import (
    ModelAcceptanceJudgeResult,
)

RUBRIC_YAML = (
    files("omnimarket")
    .joinpath("configs/delegation_acceptance_judge_rubric.v1.yaml")
    .read_text(encoding="utf-8")
)
RUBRIC = parse_rubric(RUBRIC_YAML)
EDIT_LOOP_TASK = "You are editing a git worktree to complete one task: fix the slug."


def item(
    item_id: str,
    model: str = "model-a",
    task_type: str = "document",
    task: str = "Summarise the three facts below in two sentences.",
    answer: str = "A two sentence summary.",
    kind: str = "",
    note: str = "",
) -> ModelAcceptanceItem:
    return ModelAcceptanceItem(
        item_id=item_id,
        model=model,
        task_type=task_type,
        kind=kind,
        task_text=task,
        answer_text=answer,
        note=note,
    )


def verdict(
    item_id: str, accept: bool, quality: int | None = None
) -> ModelAcceptanceVerdict:
    return ModelAcceptanceVerdict(
        item_id=item_id,
        accept=accept,
        quality=quality if quality is not None else (3 if accept else 1),
        failure_class=(
            EnumAcceptanceFailureClass.NONE
            if accept
            else EnumAcceptanceFailureClass.CONSTRAINT_VIOLATION
        ),
        reason="meets the task" if accept else "word count is off",
    )


def event(
    model: str = "model-a",
    task_type: str = "document",
    head: str = "Summarise the three facts below in two sentences.",
    chars: int = 400,
    ok: bool = True,
) -> ModelAcceptanceCellEvent:
    return ModelAcceptanceCellEvent(
        model=model,
        task_type=task_type,
        prompt_head=head,
        prompt_chars=chars,
        terminal_ok=ok,
    )


def reply(*entries: dict[str, object]) -> str:
    return json.dumps({"judgments": list(entries)})


def entry(item_id: str, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "item_id": item_id,
        "accept": True,
        "quality": 3,
        "failure_class": "none",
        "reason": "meets the task",
    }
    base.update(overrides)
    return base


def run(**fields: object) -> ModelAcceptanceJudgeResult:
    request = ModelAcceptanceJudgeRequest.model_validate(
        {"rubric_yaml": RUBRIC_YAML, **fields}
    )
    return HandlerDelegationAcceptanceJudge().handle(request)


def render(
    items: list[ModelAcceptanceItem], seed: str = "s1"
) -> ModelAcceptanceJudgeResult:
    return run(operation=EnumAcceptanceOperation.RENDER, items=tuple(items), seed=seed)


def render_capped(
    items: list[ModelAcceptanceItem], cap: int, seed: str = "s1"
) -> ModelAcceptanceJudgeResult:
    return run(
        operation=EnumAcceptanceOperation.RENDER,
        items=tuple(items),
        seed=seed,
        cell_cap=cap,
    )
