# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab-only measurement boundary. Output contains aggregate counts exclusively.

Runs node_delegation_rubric_check_compute over a JSONL export of labelled
items (one object per line with item_key, task_class, gate_verdict, label,
rater_role, prompt_snapshot and response_snapshot; an optional rubric_version is
filtered when present). The export is made on the lab host and stays there:
prompt and answer text never reach this repository or this command's output.
Execution evidence is not inferred from answer prose or gate verdicts.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import TypedDict

from omnimarket.delegation.rubric.contract_loader import (
    load_delegation_class_rubrics,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.handler_delegation_rubric_check import (
    HandlerDelegationRubricCheck,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    EnumRubricOutcome,
    ModelRubricCheckRequest,
    ModelRubricVerdict,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_lab_item import (
    ModelLabItem,
)


class _Counts(TypedDict):
    n: int
    FAIL: int
    PASS: int
    UNDETERMINED: int


class _Stratum(_Counts):
    gate_verdict: str
    label: str


class _Breakdown(TypedDict):
    counts: _Counts
    by_gate_label: list[_Stratum]


class _ClassReport(_Breakdown):
    criteria: dict[str, _Breakdown]


class _Headline(TypedDict):
    caught: int
    inadequate_accepted: int
    false_flags: int
    adequate_accepted: int


class _Report(TypedDict):
    classes: dict[str, _ClassReport]
    headline: dict[str, _Headline]


_DEFAULT_ROLE = "blind_model:claude-opus-5-5"
_DEFAULT_VERSION = "ev4-blind-v1"


def _counts(outcomes: Iterable[EnumRubricOutcome]) -> _Counts:
    counts: _Counts = {"n": 0, "FAIL": 0, "PASS": 0, "UNDETERMINED": 0}
    for outcome in outcomes:
        counts["n"] += 1
        if outcome == EnumRubricOutcome.FAIL:
            counts["FAIL"] += 1
        elif outcome == EnumRubricOutcome.PASS:
            counts["PASS"] += 1
        else:
            counts["UNDETERMINED"] += 1
    return counts


def _breakdown(rows: list[tuple[ModelLabItem, EnumRubricOutcome]]) -> _Breakdown:
    groups: dict[tuple[str, str], list[EnumRubricOutcome]] = {}
    for item, outcome in rows:
        groups.setdefault((item.gate_verdict, item.label), []).append(outcome)
    strata: list[_Stratum] = [
        {**_counts(outcomes), "gate_verdict": gate, "label": label}
        for (gate, label), outcomes in sorted(groups.items())
    ]
    return {"counts": _counts(outcome for _, outcome in rows), "by_gate_label": strata}


def measure(
    items: Iterable[ModelLabItem],
    rater_role: str = _DEFAULT_ROLE,
    rubric_version: str = _DEFAULT_VERSION,
) -> _Report:
    """Evaluate matching labelled items and retain only aggregate evidence."""
    contract = load_delegation_class_rubrics()
    handler = HandlerDelegationRubricCheck()
    groups: dict[str, list[tuple[ModelLabItem, ModelRubricVerdict]]] = {
        task_class: [] for task_class in contract.classes
    }
    for item in items:
        if item.rater_role != rater_role or item.task_class not in groups:
            continue
        if item.rubric_version is not None and item.rubric_version != rubric_version:
            continue
        verdict = handler.handle(
            ModelRubricCheckRequest(
                task_class=item.task_class,
                request_text=item.prompt_snapshot,
                answer_text=item.response_snapshot,
                rubric=contract.for_class(item.task_class),
            )
        )
        groups[item.task_class].append((item, verdict))
    report: _Report = {"classes": {}, "headline": {}}
    for task_class, rows in sorted(groups.items()):
        breakdown = _breakdown([(item, verdict.outcome) for item, verdict in rows])
        criteria = {
            criterion.criterion_id: _breakdown(
                [
                    (item, result.outcome)
                    for item, verdict in rows
                    for result in verdict.criteria
                    if result.criterion_id == criterion.criterion_id
                ]
            )
            for criterion in contract.classes[task_class]
        }
        report["classes"][task_class] = {**breakdown, "criteria": criteria}
        headline: _Headline = {
            "caught": 0,
            "inadequate_accepted": 0,
            "false_flags": 0,
            "adequate_accepted": 0,
        }
        for item, verdict in rows:
            if item.gate_verdict == "accepted":
                if item.label == "inadequate":
                    headline["inadequate_accepted"] += 1
                    headline["caught"] += verdict.outcome == EnumRubricOutcome.FAIL
                elif item.label == "adequate":
                    headline["adequate_accepted"] += 1
                    headline["false_flags"] += verdict.outcome == EnumRubricOutcome.FAIL
        report["headline"][task_class] = headline
    return report


def _jsonl_items(path: Path) -> Iterator[ModelLabItem]:
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("JSONL rows must be objects")
                # Exports can carry additional gate evidence. Only documented
                # measurement fields cross this boundary into the typed model.
                yield ModelLabItem.model_validate(
                    {key: row[key] for key in ModelLabItem.model_fields if key in row}
                )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure rubric outcomes; emit aggregate JSON counts only."
    )
    parser.add_argument("--jsonl", type=Path, required=True)
    parser.add_argument("--rater-role", default=_DEFAULT_ROLE)
    parser.add_argument("--rubric-version", default=_DEFAULT_VERSION)
    args = parser.parse_args(argv)
    try:
        report = measure(_jsonl_items(args.jsonl), args.rater_role, args.rubric_version)
    except Exception as exc:
        # Messages from validation and the driver may embed answer or DSN text,
        # so only the exception type is printed, and no partial aggregates.
        sys.stderr.write("lab_measure failed: " + type(exc).__name__ + "\n")
        return 2
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
