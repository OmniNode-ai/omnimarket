# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evaluate one interval of lab readings against the contract's indicator table.

Canonical definition-B shape: ``handle(ModelLabSliReadingBatch) ->
ModelLabSliEvaluationBatch``. Pure: no clock, no state, no I/O beyond reading
this node's own contract once.

Every number and severity comes from ``sli_table`` in ``contract.yaml``. An
indicator with no reading is ``unknown``: a source nobody reads is the failure
this node exists to show, so it is never folded into ``ok``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from omnimarket.enums.enum_lab_sli import EnumLabSliComparator, EnumLabSliStatus
from omnimarket.models.lab_sli import (
    ModelLabSliDefinition,
    ModelLabSliEvaluated,
    ModelLabSliEvaluationBatch,
    ModelLabSliReading,
    ModelLabSliReadingBatch,
    ModelLabSliSubjectReading,
)
from omnimarket.nodes.contract_topics import contract_section

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
# The one indicator this node computes itself, from its own verdicts.
COVERAGE_SLI_ID = "sli_source_coverage"


def load_sli_table(path: Path = CONTRACT_PATH) -> tuple[ModelLabSliDefinition, ...]:
    """Read and validate ``sli_table``; fail closed on a missing field or a repeat."""
    rows = contract_section(path, "sli_table")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path}: sli_table is missing or empty")
    table: list[ModelLabSliDefinition] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        try:
            definition = ModelLabSliDefinition.model_validate(row)
        except ValidationError as exc:
            raise ValueError(f"{path}: sli_table[{index}] is invalid: {exc}") from exc
        if definition.sli_id in seen:
            raise ValueError(f"{path}: sli_table repeated sli_id {definition.sli_id}")
        seen.add(definition.sli_id)
        table.append(definition)
    return tuple(table)


@lru_cache(maxsize=1)
def _contract_table() -> tuple[ModelLabSliDefinition, ...]:
    return load_sli_table()


def _breaches(definition: ModelLabSliDefinition, value: float) -> bool:
    if definition.comparator is EnumLabSliComparator.MAX:
        return value > definition.target
    return value < definition.target


def _worst(
    definition: ModelLabSliDefinition, subjects: tuple[ModelLabSliSubjectReading, ...]
) -> ModelLabSliSubjectReading:
    if definition.comparator is EnumLabSliComparator.MAX:
        return max(subjects, key=lambda s: s.value)
    return min(subjects, key=lambda s: s.value)


def evaluate_one(
    definition: ModelLabSliDefinition,
    reading: ModelLabSliReading | None,
    batch: ModelLabSliReadingBatch,
) -> ModelLabSliEvaluated:
    """The verdict of one indicator for the batch's interval."""
    base = {
        "sli_id": definition.sli_id,
        "title": definition.title,
        "unit": definition.unit,
        "severity": definition.severity,
        "target": definition.target,
        "comparator": definition.comparator,
        "window_minutes": definition.window_minutes,
        "action": definition.action,
        "interval_start": batch.interval_start,
    }
    if reading is None:
        return ModelLabSliEvaluated(
            **base, status=EnumLabSliStatus.UNKNOWN, detail="no reading from any source"
        )
    if not reading.applicable:
        return ModelLabSliEvaluated(
            **base, status=EnumLabSliStatus.NOT_APPLICABLE, detail=reading.detail
        )
    if not reading.subjects:
        return ModelLabSliEvaluated(
            **base,
            status=EnumLabSliStatus.UNKNOWN,
            detail=reading.detail or "reading carried no subjects",
        )
    worst = _worst(definition, reading.subjects)
    breaching = tuple(
        s.subject for s in reading.subjects if _breaches(definition, s.value)
    )
    return ModelLabSliEvaluated(
        **base,
        status=EnumLabSliStatus.BREACH if breaching else EnumLabSliStatus.OK,
        value=worst.value,
        worst_subject=worst.subject,
        breaching_subjects=breaching,
        subjects=reading.subjects,
        detail=reading.detail,
    )


class HandlerLabSliEvaluate:
    """One verdict per indicator in the contract table, in table order."""

    def __init__(self, table: tuple[ModelLabSliDefinition, ...] | None = None) -> None:
        self._table = table if table is not None else _contract_table()

    def handle(self, request: ModelLabSliReadingBatch) -> ModelLabSliEvaluationBatch:
        known = {d.sli_id for d in self._table}
        readings: dict[str, ModelLabSliReading] = {}
        rejected: list[str] = []
        for reading in request.readings:
            if reading.sli_id not in known:
                rejected.append(f"unknown:{reading.sli_id}")
            elif reading.sli_id in readings:
                rejected.append(f"duplicate:{reading.sli_id}")
            else:
                readings[reading.sli_id] = reading

        measured = {
            d.sli_id: evaluate_one(d, readings.get(d.sli_id), request)
            for d in self._table
            if d.sli_id != COVERAGE_SLI_ID
        }
        evaluations: list[ModelLabSliEvaluated] = []
        for definition in self._table:
            if definition.sli_id == COVERAGE_SLI_ID and COVERAGE_SLI_ID not in readings:
                evaluations.append(self._coverage(definition, measured, request))
            elif definition.sli_id == COVERAGE_SLI_ID:
                evaluations.append(
                    evaluate_one(definition, readings[COVERAGE_SLI_ID], request)
                )
            else:
                evaluations.append(measured[definition.sli_id])
        return ModelLabSliEvaluationBatch(
            interval_start=request.interval_start,
            collector_host=request.collector_host,
            evaluations=tuple(evaluations),
            rejected=tuple(sorted(rejected)),
            source_errors=request.source_errors,
        )

    @staticmethod
    def _coverage(
        definition: ModelLabSliDefinition,
        measured: dict[str, ModelLabSliEvaluated],
        request: ModelLabSliReadingBatch,
    ) -> ModelLabSliEvaluated:
        dark = sorted(
            k for k, e in measured.items() if e.status is EnumLabSliStatus.UNKNOWN
        )
        ratio = (len(measured) - len(dark)) / len(measured) if measured else 0.0
        reading = ModelLabSliReading(
            sli_id=definition.sli_id,
            subjects=(ModelLabSliSubjectReading(subject="lab", value=ratio),),
            detail="dark: " + ", ".join(dark) if dark else "every indicator read",
        )
        return evaluate_one(definition, reading, request)


__all__ = ["COVERAGE_SLI_ID", "HandlerLabSliEvaluate", "evaluate_one", "load_sli_table"]
