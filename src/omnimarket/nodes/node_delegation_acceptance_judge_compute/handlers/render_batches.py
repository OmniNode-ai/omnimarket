# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render judge batches from items: probe exclusion, shortening, batching and the sample. Pure."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict

from omnimarket.models.delegation_acceptance_judge.model_acceptance_item import (
    ModelAcceptanceItem,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.classify import (
    class_key,
    is_probe,
    kind_of,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_batch import (
    ModelAcceptanceBatch,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_rubric import (
    ModelAcceptanceRubric,
)


def _digest(seed: str, salt: str, item_id: str) -> str:
    return hashlib.sha256(f"{seed}|{salt}|{item_id}".encode()).hexdigest()


def shorten(text: str, cap: int, head: int, tail: int) -> tuple[str, int]:
    """Keep the head and tail of a text longer than the cap; return it and the omitted count."""
    if len(text) <= cap:
        return text, 0
    omitted = len(text) - head - tail
    return (
        f"{text[:head]}\n[... {omitted} characters omitted ...]\n{text[len(text) - tail :]}",
        omitted,
    )


def _item_block(item: ModelAcceptanceItem, rubric: ModelAcceptanceRubric) -> str:
    kind = kind_of(item.task_text, item.kind, rubric)
    batching = rubric.batching
    task, task_omitted = shorten(
        item.task_text,
        batching.task_char_cap,
        batching.task_head_chars,
        batching.task_tail_chars,
    )
    answer, answer_omitted = shorten(
        item.answer_text,
        batching.answer_char_cap,
        batching.answer_head_chars,
        batching.answer_tail_chars,
    )
    notes = [item.note] if item.note else []
    if task_omitted:
        notes.append(
            f"the task was shortened for judging ({task_omitted} characters omitted)"
        )
    if answer_omitted:
        notes.append(
            f"the output was shortened for judging ({answer_omitted} characters omitted)"
        )
    label = f"routing class: {item.task_type}"
    if kind == rubric.edit_loop.kind:
        label += f", kind: {kind}"
    lines = [f"=== ITEM {item.item_id} | {label} ==="]
    if notes:
        lines.append(f"[note: {'; '.join(notes)}]")
    lines += [
        "--- TASK ---",
        task,
        "--- OUTPUT ---",
        answer,
        f"=== END ITEM {item.item_id} ===",
    ]
    return "\n".join(lines)


def _class_notes(keys: list[str], rubric: ModelAcceptanceRubric) -> str:
    sections = []
    for key in keys:
        class_rubric = rubric.classes[key]
        accept = "\n".join(f"  - {line}" for line in class_rubric.accept)
        rejects = "\n".join(f"  - {line}" for line in class_rubric.reject_notes)
        sections.append(
            f"Class {key}:\n Accept when:\n{accept}\n Typical rejects:\n{rejects}"
        )
    return (
        "Per routing class (the class label is the caller's routing guess and can be wrong; "
        "always judge against what the task text actually asks):\n\n"
        + "\n\n".join(sections)
    )


def render_prompt(
    items: list[ModelAcceptanceItem], rubric: ModelAcceptanceRubric
) -> str:
    """The full judge prompt for one batch. It never contains a model name."""
    prompt = rubric.judge_prompt
    reject_lines = "\n".join(
        f"- {condition.failure_class.value}: {condition.text}"
        for condition in prompt.reject_conditions
    )
    keys: list[str] = []
    for item in items:
        key = class_key(
            item.task_type, kind_of(item.task_text, item.kind, rubric), rubric
        )
        if key is not None and key not in keys:
            keys.append(key)
    sections = [
        prompt.preamble.strip(),
        prompt.decision.strip(),
        f"{prompt.reject_intro}\n{reject_lines}",
        prompt.permitted_reply_rule.strip(),
    ]
    if keys:
        sections.append(_class_notes(keys, rubric))
    sections += [
        prompt.omission_rule.strip(),
        prompt.quality_scale.strip(),
        prompt.failure_class_rule.strip(),
        prompt.reason_rule.format(reason_max_chars=rubric.thresholds.reason_max_chars),
        prompt.closing_rule.strip(),
        prompt.output_shape.strip(),
        f"ITEMS ({len(items)}):",
        "\n\n".join(_item_block(item, rubric) for item in items),
    ]
    return "\n\n".join(sections) + "\n"


def _chunks(
    ordered: list[ModelAcceptanceItem], role: str, rubric: ModelAcceptanceRubric
) -> list[ModelAcceptanceBatch]:
    size = rubric.batching.max_items_per_batch
    return [
        ModelAcceptanceBatch(
            batch_id=f"{role}-{index // size + 1:03d}",
            role=role,
            item_ids=tuple(item.item_id for item in ordered[index : index + size]),
            prompt=render_prompt(ordered[index : index + size], rubric),
        )
        for index in range(0, len(ordered), size)
    ]


def split_probes(
    items: tuple[ModelAcceptanceItem, ...], rubric: ModelAcceptanceRubric
) -> tuple[list[ModelAcceptanceItem], list[str]]:
    kept: list[ModelAcceptanceItem] = []
    probes: list[str] = []
    for item in items:
        if is_probe(item.task_text, len(item.task_text), rubric):
            probes.append(item.item_id)
        else:
            kept.append(item)
    return kept, probes


def double_judge_sample(
    items: list[ModelAcceptanceItem], seed: str, rubric: ModelAcceptanceRubric
) -> list[ModelAcceptanceItem]:
    """Per cell, the first ceil(fraction * n) items (at least the floor) in seeded hash order."""
    cells: dict[tuple[str, str, str], list[ModelAcceptanceItem]] = defaultdict(list)
    for item in items:
        kind = kind_of(item.task_text, item.kind, rubric)
        cells[(item.model, item.task_type, kind)].append(item)
    thresholds = rubric.thresholds
    picked: list[ModelAcceptanceItem] = []
    for cell_items in cells.values():
        ranked = sorted(
            cell_items, key=lambda i: (_digest(seed, "sample", i.item_id), i.item_id)
        )
        count = min(
            len(ranked),
            max(
                thresholds.double_judge_min_per_cell,
                math.ceil(thresholds.double_judge_fraction * len(ranked)),
            ),
        )
        picked.extend(ranked[:count])
    return sorted(picked, key=lambda i: (_digest(seed, "order2", i.item_id), i.item_id))


def cap_cells(
    items: list[ModelAcceptanceItem], cap: int, seed: str, rubric: ModelAcceptanceRubric
) -> tuple[list[ModelAcceptanceItem], list[str]]:
    """Keep the first ``cap`` items of each cell in seeded hash order; return the kept and dropped."""
    if cap == 0:
        return items, []
    cells: dict[tuple[str, str, str], list[ModelAcceptanceItem]] = defaultdict(list)
    for item in items:
        kind = kind_of(item.task_text, item.kind, rubric)
        cells[(item.model, item.task_type, kind)].append(item)
    kept: list[ModelAcceptanceItem] = []
    dropped: list[str] = []
    for cell_items in cells.values():
        ranked = sorted(
            cell_items, key=lambda i: (_digest(seed, "cap", i.item_id), i.item_id)
        )
        kept.extend(ranked[:cap])
        dropped.extend(i.item_id for i in ranked[cap:])
    return kept, sorted(dropped)


def render_batches(
    items: tuple[ModelAcceptanceItem, ...],
    seed: str,
    rubric: ModelAcceptanceRubric,
    cell_cap: int = 0,
) -> tuple[list[ModelAcceptanceBatch], list[str], list[str], list[ModelAcceptanceItem]]:
    ids = [item.item_id for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError("item ids must be unique")
    candidates, probe_ids = split_probes(items, rubric)
    if not candidates:
        raise ValueError("every item is a probe: nothing to judge")
    kept, dropped = cap_cells(candidates, cell_cap, seed, rubric)
    ordered = sorted(kept, key=lambda i: (_digest(seed, "order", i.item_id), i.item_id))
    sample = double_judge_sample(kept, seed, rubric)
    batches = _chunks(ordered, "primary", rubric) + _chunks(sample, "secondary", rubric)
    return batches, probe_ids, dropped, sample
