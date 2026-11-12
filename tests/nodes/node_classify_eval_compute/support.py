# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for the node_classify_eval_compute tests."""

from __future__ import annotations

from omnimarket.models.eval_classify.enum_classify import EnumIntegrationClass
from omnimarket.models.eval_classify.model_classify_eval import ModelClassifyAnswer
from omnimarket.models.eval_classify.model_classify_overlay import (
    ModelClassifyEvalOverlay,
)
from omnimarket.models.eval_classify.model_classify_task import ModelClassifyEvalTask

C = EnumIntegrationClass

SECTION_LABELS: dict[str, EnumIntegrationClass] = {
    "NOT RUNNING": C.NOT_RUNNING,
    "BLOCKED": C.BLOCKED,
    "DEFECT": C.DEFECT,
    "SEEN RUNNING": C.SEEN_RUNNING,
    "IN PLACE OR UNPROVEN": C.RUN_NOT_SEEN,
}


def overlay(
    per_class_n: dict[EnumIntegrationClass, int] | None = None,
    min_total: int = 8,
) -> ModelClassifyEvalOverlay:
    return ModelClassifyEvalOverlay(
        section_labels=SECTION_LABELS,
        per_class_n=per_class_n
        or {
            C.NOT_RUNNING: 2,
            C.BLOCKED: 1,
            C.DEFECT: 1,
            C.SEEN_RUNNING: 2,
            C.RUN_NOT_SEEN: 2,
        },
        seed="s",
        min_total=min_total,
        rubric="Classify one merged change. Last line: the class word alone.",
    )


def _state(repo: str, number: str, title: str, state: str) -> str:
    return (
        f"- {repo} {number} | {title} | merged 2026-11-05 01:41Z | runs: dev runtime"
        f" | state: {state} | gap: gap text of {title} | step: step text | owner: NONE"
    )


def _seen(repo: str, number: str, title: str) -> str:
    return f"- {repo} {number} | {title} | merged 2026-11-06 02:00Z | reading: reading text of {title}"


def _owed(repo: str, number: str, title: str, state: str) -> str:
    return (
        f"- {repo} {number} | {title} | merged 2026-11-07 03:00Z | runs: skills"
        f" | state: {state} | reading owed: owed text of {title}"
    )


LEDGER_TEXT = "\n".join(
    [
        "# Integration ledger",
        "- a line before any section is never a task",
        "## NOT RUNNING (3, oldest merge first)",
        _state("repo-a", "1", "nr one", "NOT RUNNING"),
        _state("repo-a", "2", "nr two", "NOT RUNNING"),
        _state("overlays", "(no number)", "nr no number", "NOT RUNNING"),
        "## BLOCKED (1, oldest merge first)",
        _state("repo-b", "3", "blocked one", "BLOCKED"),
        "## DEFECT (1, oldest merge first)",
        _state("repo-b", "4", "defect one", "DEFECT"),
        "## SEEN RUNNING (3)",
        _seen("repo-c", "5", "seen one"),
        _seen("repo-c", "6", "seen two"),
        _seen("repo-c", "7", "seen three"),
        "## IN PLACE OR UNPROVEN, RUN NOT SEEN (3; not counted)",
        _owed("repo-d", "8", "rns one", "UNPROVEN"),
        _owed("repo-d", "9", "rns two", "IN PLACE"),
        _owed("repo-d", "10", "rns three", "IN PLACE"),
        "## COUNTS",
        "- merged since 2026-11-05T00:00Z: 11; this heading maps to no class",
    ]
)


def task(task_id: str, label: EnumIntegrationClass) -> ModelClassifyEvalTask:
    return ModelClassifyEvalTask(
        task_id=task_id,
        label=label,
        repository="repo-x",
        title=f"title of {task_id}",
        merged_at="2026-11-05 01:41Z",
        runs="dev runtime",
        evidence="evidence",
    )


def answer(
    task_id: str,
    reply: str,
    engine: str = "e1",
    variant: str = "full",
    seconds: float = 1.0,
) -> ModelClassifyAnswer:
    return ModelClassifyAnswer(
        engine=engine,
        input_variant=variant,
        task_id=task_id,
        reply=reply,
        seconds=seconds,
    )
