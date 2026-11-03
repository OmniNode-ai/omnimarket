# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure work progress judgment over caller-supplied facts and budgets."""

from __future__ import annotations

from omnimarket.nodes.node_work_progress_compute.models.model_work_progress import (
    ModelWorkProgressFinding,
    ModelWorkProgressRequest,
    ModelWorkProgressResult,
)


class HandlerWorkProgress:
    """Report each subject whose elapsed time strictly exceeds its phase budget."""

    def handle(self, request: ModelWorkProgressRequest) -> ModelWorkProgressResult:
        findings: list[ModelWorkProgressFinding] = []
        for subject in request.subjects:
            elapsed = (request.now - subject.entered_at).total_seconds()
            budget = request.budgets[subject.phase]
            if elapsed <= budget:
                continue
            spent = int(elapsed)
            phase_note = request.phase_notes.get(subject.phase, "")
            note = f"; {phase_note}" if phase_note else ""
            findings.append(
                ModelWorkProgressFinding(
                    idempotency_key=(
                        f"work-progress|lane:{subject.lane}|"
                        f"{subject.phase}|{subject.entering_row_id}"
                    ),
                    lane=subject.lane,
                    phase=subject.phase,
                    entering_row_id=subject.entering_row_id,
                    budget_seconds=budget,
                    spent_seconds=spent,
                    owner=subject.owner,
                    text=(
                        f"Lane {subject.lane} has been in {subject.phase} for "
                        f"{spent // 60} minutes against a {budget // 60} minute "
                        f"budget{note}. Owner: {subject.owner}."
                    ),
                )
            )
        return ModelWorkProgressResult(findings=tuple(findings))


__all__ = ["HandlerWorkProgress"]
