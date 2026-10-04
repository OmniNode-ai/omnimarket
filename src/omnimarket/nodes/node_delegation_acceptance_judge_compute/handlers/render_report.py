# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The markdown report of a scored run. Pure formatting of numbers the node computed."""

from __future__ import annotations

from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_agreement import (
    ModelAcceptanceAgreement,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_issue import (
    ModelAcceptanceIssue,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_matrix_row import (
    ModelAcceptanceMatrixRow,
)


def _pct(value: float | None) -> str:
    return "" if value is None else f"{value * 100:.0f}%"


def render_report(
    rubric_version: str,
    issues: list[ModelAcceptanceIssue],
    agreement: ModelAcceptanceAgreement,
    rows: list[ModelAcceptanceMatrixRow],
    events_counted: int,
    accept_rate: float | None,
    terminal_ok_rate: float | None,
    items_judged: int,
    receipts_joined: int,
) -> str:
    lines = ["# Judged acceptance of delegated output, by model and task type", ""]
    if issues:
        lines += [
            "**UNCALIBRATED: THIS RUN FAILED ITS GATE.** Do not route on this matrix. "
            "The rows are shown only so the failure can be read.",
            "",
            *(
                f"- `{i.code.value}`{' ' + i.item_id if i.item_id else ''}: {i.message}"
                for i in issues
            ),
            "",
        ]
    lines += [
        f"Rubric `{rubric_version}`. {items_judged} items judged; "
        f"{receipts_joined} joined to a run receipt on the judging machine.",
        "",
        f"Agreement of the two judges on {agreement.double_judged} double-judged items "
        f"(the run needs {agreement.required_double_judged}): {agreement.agreed} agree "
        f"({_pct(agreement.agreement)}), Cohen kappa {agreement.kappa:.2f}, minimum {agreement.kappa_min:.2f}.",
        "",
    ]
    if accept_rate is not None:
        lines += [
            f"Weighted by event volume over {events_counted} events: {_pct(accept_rate)} accepted "
            f"against `terminal_ok` {_pct(terminal_ok_rate)}.",
            "",
        ]
    lines += [
        "| model | task type | kind | judged | accepted | accept rate (95% CI) | mean quality "
        "| cell events | terminal_ok | terminal_ok x accept | thin |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row.model} | {row.task_type} | {row.kind} | {row.judged} | {row.accepted} "
            f"| {_pct(row.accept_rate)} ({row.accept_low * 100:.0f}-{row.accept_high * 100:.0f}) "
            f"| {row.mean_quality:.2f} | {row.cell_events} | {_pct(row.terminal_ok_rate)} "
            f"| {_pct(row.usable_rate)} | {'yes' if row.thin else ''} |"
        )
    lines += [
        "",
        "A thin cell has fewer judged items than the rubric minimum and carries no routing weight.",
        "",
    ]
    rejects = [
        f"- {row.model} / {row.task_type} / {row.kind}: "
        + ", ".join(f"{f.failure_class.value} {f.count}" for f in row.failures)
        for row in rows
        if row.failures
    ]
    if rejects:
        lines += ["## Rejects by failure class", "", *rejects, ""]
    return "\n".join(lines)
