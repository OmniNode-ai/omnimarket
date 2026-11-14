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
from omnimarket.nodes.node_pr_landing_tick_observe_compute import diff_paths

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

# Parts of the next state that one pull request or cause owns, and the field that names the owner.
NEXT_STATE_SUBJECT_FIELDS: Final[dict[str, str]] = {"leases": "pr", "records": "pr"}


def fold_decision(decision: ModelLandingDecision) -> dict[str, dict[str, Any]]:
    """Group a decision's rows by subject; the rest of the decision is the tick-level subject."""
    doc: dict[str, Any] = decision.model_dump(mode="json")
    grouped: dict[str, dict[str, Any]] = defaultdict(dict)
    for section, field in NEXT_STATE_SUBJECT_FIELDS.items():
        for row in doc["next_state"].pop(section):
            grouped[row[field]].setdefault(f"next_state_{section}", []).append(row)
    for section, field in SUBJECT_FIELDS.items():
        for row in doc.pop(section):
            grouped[row[field]].setdefault(section, []).append(row)
    grouped[TICK_LEVEL_SUBJECT] = doc
    return dict(grouped)


def _action_order(decision: ModelLandingDecision) -> list[tuple[str, str]]:
    return [(a.subject, str(a.kind)) for a in decision.actions]


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
        # Actions run in order across subjects: the same actions in another global order is a
        # tick-level difference (a different set of actions is already named per subject).
        node_order = _action_order(request.decision)
        recorded_order = _action_order(request.recorded)
        if node_order != recorded_order and sorted(node_order) == sorted(
            recorded_order
        ):
            order = ModelTickDifference(
                path="action_order",
                recorded=json.dumps(recorded_order),
                node=json.dumps(node_order),
            )
            tick_rows = [d for d in divergences if d.subject == TICK_LEVEL_SUBJECT]
            divergences = [d for d in divergences if d.subject != TICK_LEVEL_SUBJECT]
            divergences.append(
                ModelTickDivergence(
                    subject=TICK_LEVEL_SUBJECT,
                    differences=(
                        *(tick_rows[0].differences if tick_rows else ()),
                        order,
                    ),
                )
            )
            divergences.sort(key=lambda d: d.subject)
        return ModelTickReduceOutput(
            tick=request.decision.tick,
            rows=rows,
            divergences=tuple(divergences),
            compared=True,
        )


__all__: list[str] = [
    "NEXT_STATE_SUBJECT_FIELDS",
    "SUBJECT_FIELDS",
    "HandlerPrLandingTickReducer",
    "fold_decision",
]
