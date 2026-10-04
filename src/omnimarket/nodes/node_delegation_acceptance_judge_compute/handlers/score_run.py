# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Score a judged run: agreement gate and the model by task type matrix. Pure."""

from __future__ import annotations

import math
from collections import Counter, defaultdict

from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.classify import (
    is_probe,
    kind_of,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.stats import (
    cohen_kappa,
    wilson_accept_interval,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_failure_class import (
    EnumAcceptanceFailureClass,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_issue_code import (
    EnumAcceptanceIssueCode as Code,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_agreement import (
    ModelAcceptanceAgreement,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_cell_event import (
    ModelAcceptanceCellEvent,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_failure_count import (
    ModelAcceptanceFailureCount,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_issue import (
    ModelAcceptanceIssue,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_item import (
    ModelAcceptanceItem,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_matrix_row import (
    ModelAcceptanceMatrixRow,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_rubric import (
    ModelAcceptanceRubric,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_verdict import (
    ModelAcceptanceVerdict,
)

_Cell = tuple[str, str, str]


def _index(
    verdicts: tuple[ModelAcceptanceVerdict, ...],
    known: set[str],
    role: str,
    issues: list[ModelAcceptanceIssue],
) -> dict[str, ModelAcceptanceVerdict]:
    counts = Counter(v.item_id for v in verdicts)
    indexed: dict[str, ModelAcceptanceVerdict] = {}
    for verdict in verdicts:
        if verdict.item_id not in known:
            issues.append(
                ModelAcceptanceIssue(
                    code=Code.UNKNOWN_ITEM,
                    item_id=verdict.item_id,
                    message=f"a {role} verdict names an item that is not in the run",
                )
            )
        elif counts[verdict.item_id] > 1:
            if verdict.item_id not in indexed:
                issues.append(
                    ModelAcceptanceIssue(
                        code=Code.DUPLICATE_ITEM,
                        item_id=verdict.item_id,
                        message=f"{counts[verdict.item_id]} {role} verdicts for one item",
                    )
                )
                indexed[verdict.item_id] = verdict
        else:
            indexed[verdict.item_id] = verdict
    return indexed


def _agreement(
    primary: dict[str, ModelAcceptanceVerdict],
    secondary: dict[str, ModelAcceptanceVerdict],
    judged_total: int,
    rubric: ModelAcceptanceRubric,
    issues: list[ModelAcceptanceIssue],
) -> ModelAcceptanceAgreement:
    thresholds = rubric.thresholds
    required = math.ceil(thresholds.double_judge_fraction * judged_total)
    pairs = [
        (primary[i].accept, secondary[i].accept)
        for i in sorted(primary)
        if i in secondary
    ]
    if not pairs:
        issues.append(
            ModelAcceptanceIssue(
                code=Code.NO_DOUBLE_JUDGED_ITEMS,
                message="no item was judged by both judges, so agreement is unmeasured",
            )
        )
        return ModelAcceptanceAgreement(
            double_judged=0,
            required_double_judged=required,
            agreed=0,
            agreement=0.0,
            kappa=0.0,
            kappa_min=thresholds.kappa_min,
        )
    agreed = sum(a == b for a, b in pairs)
    kappa = cohen_kappa(pairs)
    if len(pairs) < required:
        issues.append(
            ModelAcceptanceIssue(
                code=Code.SAMPLE_TOO_SMALL,
                message=(
                    f"{len(pairs)} double-judged items, the run needs at least {required} "
                    f"({thresholds.double_judge_fraction:.0%} of {judged_total})"
                ),
            )
        )
    if kappa < thresholds.kappa_min:
        issues.append(
            ModelAcceptanceIssue(
                code=Code.KAPPA_BELOW_MINIMUM,
                message=(
                    f"kappa {kappa:.2f} between the judges is under {thresholds.kappa_min:.2f}: "
                    "the matrix is not calibrated"
                ),
            )
        )
    return ModelAcceptanceAgreement(
        double_judged=len(pairs),
        required_double_judged=required,
        agreed=agreed,
        agreement=agreed / len(pairs),
        kappa=kappa,
        kappa_min=thresholds.kappa_min,
    )


def _event_totals(
    events: tuple[ModelAcceptanceCellEvent, ...], rubric: ModelAcceptanceRubric
) -> dict[_Cell, tuple[int, int]]:
    totals: dict[_Cell, list[int]] = defaultdict(lambda: [0, 0])
    for event in events:
        if is_probe(event.prompt_head, event.prompt_chars, rubric):
            continue
        key = (event.model, event.task_type, kind_of(event.prompt_head, "", rubric))
        totals[key][0] += 1
        totals[key][1] += int(event.terminal_ok)
    return {key: (count, ok) for key, (count, ok) in totals.items()}


def score_run(
    items: tuple[ModelAcceptanceItem, ...],
    primary_verdicts: tuple[ModelAcceptanceVerdict, ...],
    secondary_verdicts: tuple[ModelAcceptanceVerdict, ...],
    events: tuple[ModelAcceptanceCellEvent, ...],
    rubric: ModelAcceptanceRubric,
) -> tuple[
    ModelAcceptanceAgreement,
    list[ModelAcceptanceMatrixRow],
    int,
    float | None,
    float | None,
    list[ModelAcceptanceIssue],
]:
    issues: list[ModelAcceptanceIssue] = []
    judged_items = [
        item
        for item in items
        if not is_probe(item.task_text, len(item.task_text), rubric)
    ]
    known = {item.item_id for item in judged_items}
    primary = _index(primary_verdicts, known, "primary", issues)
    secondary = _index(secondary_verdicts, known, "second-judge", issues)
    for item in judged_items:
        if item.item_id not in primary:
            issues.append(
                ModelAcceptanceIssue(
                    code=Code.PRIMARY_VERDICT_MISSING,
                    item_id=item.item_id,
                    message="the primary judge has no verdict for this item",
                )
            )
    agreement = _agreement(primary, secondary, len(judged_items), rubric, issues)

    cells: dict[_Cell, list[ModelAcceptanceVerdict]] = defaultdict(list)
    for item in judged_items:
        verdict = primary.get(item.item_id)
        if verdict is not None:
            key = (
                item.model,
                item.task_type,
                kind_of(item.task_text, item.kind, rubric),
            )
            cells[key].append(verdict)
    totals = _event_totals(events, rubric)
    rows: list[ModelAcceptanceMatrixRow] = []
    weighted_accept = 0.0
    weighted_events = 0
    weighted_ok = 0
    for key in sorted(cells):
        verdicts = cells[key]
        accepted = sum(v.accept for v in verdicts)
        low, high = wilson_accept_interval(accepted, len(verdicts))
        cell_events, cell_ok = totals.get(key, (0, 0))
        accept_rate = accepted / len(verdicts)
        ok_rate = cell_ok / cell_events if cell_events else None
        failures = Counter(
            v.failure_class
            for v in verdicts
            if not v.accept and v.failure_class is not EnumAcceptanceFailureClass.NONE
        )
        rows.append(
            ModelAcceptanceMatrixRow(
                model=key[0],
                task_type=key[1],
                kind=key[2],
                judged=len(verdicts),
                accepted=accepted,
                accept_rate=accept_rate,
                accept_low=low,
                accept_high=high,
                mean_quality=sum(v.quality for v in verdicts) / len(verdicts),
                cell_events=cell_events,
                terminal_ok_rate=ok_rate,
                usable_rate=None if ok_rate is None else ok_rate * accept_rate,
                thin=len(verdicts) < rubric.thresholds.min_judged_per_cell,
                failures=tuple(
                    ModelAcceptanceFailureCount(failure_class=cls, count=count)
                    for cls, count in sorted(
                        failures.items(), key=lambda pair: (-pair[1], pair[0].value)
                    )
                ),
            )
        )
        weighted_accept += accept_rate * cell_events
        weighted_events += cell_events
        weighted_ok += cell_ok
    if weighted_events:
        return (
            agreement,
            rows,
            weighted_events,
            weighted_accept / weighted_events,
            weighted_ok / weighted_events,
            issues,
        )
    return agreement, rows, 0, None, None, issues
