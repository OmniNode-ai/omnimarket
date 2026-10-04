# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Probe, kind and class recognition from the rubric's patterns. Pure."""

from __future__ import annotations

import re

from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_rubric import (
    ModelAcceptanceRubric,
)

TASK_KIND = "task"


def is_probe(task_text: str, task_chars: int, rubric: ModelAcceptanceRubric) -> bool:
    """A short single-word task: it tests that a backend answers, not that it works."""
    if task_chars > rubric.probes.max_task_chars:
        return False
    return any(
        re.search(pattern, task_text, re.IGNORECASE)
        for pattern in rubric.probes.task_patterns
    )


def kind_of(task_text: str, declared: str, rubric: ModelAcceptanceRubric) -> str:
    """The declared kind, else edit_loop_turn when the task opens as an editing-loop turn."""
    if declared:
        return declared
    if any(re.search(pattern, task_text) for pattern in rubric.edit_loop.task_patterns):
        return rubric.edit_loop.kind
    return TASK_KIND


def class_key(task_type: str, kind: str, rubric: ModelAcceptanceRubric) -> str | None:
    """The rubric class that judges this item, or None when only the global rules apply."""
    if kind == rubric.edit_loop.kind:
        return kind
    for key, class_rubric in rubric.classes.items():
        if task_type in class_rubric.routing_labels:
            return key
    return None
