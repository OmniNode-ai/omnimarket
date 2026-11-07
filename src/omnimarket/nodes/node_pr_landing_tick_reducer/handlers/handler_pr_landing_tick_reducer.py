# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Landing tick reducer: one decision folded to a row per subject, compared subject by subject.

Pure definition-B reducer. The decision's sections (actions, observed actions, outcomes, gates,
land skips, degraded rows, violations, companion verdicts) are grouped by the pull request or
cause they name; everything else in the decision (the tick number, the observe-only refusals and
the next state) is the tick-level row. A recorded decision is folded the same way and the two
documents are compared per subject, so a difference names the pull request it is about.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Final

from omnimarket.models.landing_decision import ModelLandingDecision
from omnimarket.models.landing_tick import (
    TICK_LEVEL_SUBJECT,
    ModelTickDifference,
    ModelTickDivergence,
    ModelTickReduceInput,
    ModelTickReduceOutput,
    ModelTickSubjectRow,
)
from omnimarket.nodes.node_pr_landing_tick_observe_compute.handlers.handler_pr_landing_tick_observe import (
    diff_paths,
)

# Section name -> the field that names the subject of each of its rows.
SUBJECT_FIELDS: Final[dict[str, str]] = {
    "actions": "subject",
    "observed_actions": "subject",
    "recorded_outcomes": "pr",
    "gates": "pr",
    "land_skips": "pr",
    "degraded": "subject",
    "violations": "pr",
    "companion_verdicts": "companion",
}


def fold_decision(decision: ModelLandingDecision) -> dict[str, dict[str, Any]]:
    """Group a decision's rows by subject; the rest of the decision is the tick-level subject."""
    doc: dict[str, Any] = decision.model_dump(mode="json")
    grouped: dict[str, dict[str, Any]] = defaultdict(dict)
    for section, field in SUBJECT_FIELDS.items():
        for row in doc.pop(section):
            grouped[row[field]].setdefault(section, []).append(row)
    grouped[TICK_LEVEL_SUBJECT] = doc
    return dict(grouped)


def _kinds(section_rows: dict[str, Any]) -> tuple[str, ...]:
    return tuple(row["kind"] for row in section_rows.get("actions", ()))


class HandlerPrLandingTickReducer:
    """Fold a tick's decision per subject and name each subject that differs from the record."""

    def handle(self, request: ModelTickReduceInput) -> ModelTickReduceOutput:
        node = fold_decision(request.decision)
        rows = tuple(
            ModelTickSubjectRow(
                subject=subject,
                action_kinds=_kinds(node[subject]),
                document=json.dumps(node[subject], sort_keys=True),
            )
            for subject in sorted(node)
        )
        if request.recorded is None:
            return ModelTickReduceOutput(
                tick=request.decision.tick, rows=rows, compared=False
            )
        recorded = fold_decision(request.recorded)
        divergences: list[ModelTickDivergence] = []
        for subject in sorted({*node, *recorded}):
            differences = tuple(
                ModelTickDifference(path=d.path, recorded=d.controller, node=d.node)
                for d in diff_paths(recorded.get(subject, {}), node.get(subject, {}))
            )
            if differences:
                divergences.append(
                    ModelTickDivergence(subject=subject, differences=differences)
                )
        return ModelTickReduceOutput(
            tick=request.decision.tick,
            rows=rows,
            divergences=tuple(divergences),
            compared=True,
        )


__all__: list[str] = ["SUBJECT_FIELDS", "HandlerPrLandingTickReducer", "fold_decision"]
