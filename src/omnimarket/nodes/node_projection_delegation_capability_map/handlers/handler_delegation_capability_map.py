# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure fold of capability verdict rows into one cell per model, backend,
configuration, class and band (OMN-20169, plan task RS.5).

No clock, database, broker or model call. The result is a function of the
request: arrival order and redelivery change nothing. A row is admitted to a
cell only when its six-field identity matches the serving assignment of its
model, backend and configuration (OMN-18537); a row with no configuration, a
missing identity field, no assignment or a differing field folds nowhere and
is counted by reason. A cell below the line's required n reads REFUSED, and a
band with no rows has no cell.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from uuid import UUID

from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation_capability_map.enum_capability_cell_verdict import (
    EnumCapabilityCellVerdict,
)
from omnimarket.models.delegation_capability_map.enum_capability_inadmissible_reason import (
    EnumCapabilityInadmissibleReason,
)
from omnimarket.models.delegation_capability_map.model_capability_best_configuration import (
    ModelCapabilityBestConfiguration,
)
from omnimarket.models.delegation_capability_map.model_capability_cell import (
    ModelCapabilityCell,
)
from omnimarket.models.delegation_capability_map.model_capability_inadmissible_count import (
    ModelCapabilityInadmissibleCount,
)
from omnimarket.models.delegation_capability_map.model_capability_line import (
    ModelCapabilityLine,
)
from omnimarket.models.delegation_capability_map.model_capability_map_fold import (
    ModelCapabilityMapFoldRequest,
    ModelCapabilityMapFoldResult,
)
from omnimarket.models.delegation_capability_map.model_capability_serving_assignment import (
    ModelCapabilityServingAssignment,
)
from omnimarket.models.delegation_capability_map.model_capability_verdict_row import (
    ModelCapabilityVerdictRow,
)
from omnimarket.ranges.power import decision_lower_bound, required_sample_size

_Z = 1.959963984540054

_CellKey = tuple[UUID, str, str, str, str, EnumSizeBand]
_AssignmentKey = tuple[str, str, str]
_RowKey = tuple[UUID, str, str, str, str, str]


def _wilson_interval(passes: int, n: int) -> tuple[float, float]:
    """Two-sided Wilson 95 percent interval for a pass proportion."""
    p = passes / n
    denominator = 1 + _Z**2 / n
    center = (p + _Z**2 / (2 * n)) / denominator
    half = _Z * math.sqrt(p * (1 - p) / n + _Z**2 / (4 * n**2)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def _admissibility(
    row: ModelCapabilityVerdictRow,
    assignments: dict[_AssignmentKey, ModelCapabilityServingAssignment],
) -> EnumCapabilityInadmissibleReason | None:
    if row.configuration is None:
        return EnumCapabilityInadmissibleReason.NO_CONFIGURATION
    if row.identity.missing_fields():
        return EnumCapabilityInadmissibleReason.IDENTITY_FIELD_MISSING
    assignment = assignments.get(
        (row.model_key, row.backend_id, row.configuration.configuration_id)
    )
    if assignment is None:
        return EnumCapabilityInadmissibleReason.NO_SERVING_ASSIGNMENT
    if row.identity.differing_fields(assignment.identity):
        return EnumCapabilityInadmissibleReason.IDENTITY_MISMATCH
    return None


def _row_key(row: ModelCapabilityVerdictRow, configuration_id: str) -> _RowKey:
    return (
        row.tenant_id,
        row.item_key,
        row.source.value,
        row.model_key,
        row.backend_id,
        configuration_id,
    )


def _cell_key(row: ModelCapabilityVerdictRow, configuration_id: str) -> _CellKey:
    return (
        row.tenant_id,
        row.model_key,
        row.backend_id,
        configuration_id,
        row.task_class,
        row.size_band,
    )


class HandlerDelegationCapabilityMap:
    """Fold one map run's verdict rows into capability cells."""

    def handle(
        self, request: ModelCapabilityMapFoldRequest
    ) -> ModelCapabilityMapFoldResult:
        assignments = {
            (a.model_key, a.backend_id, a.configuration_id): a
            for a in sorted(request.assignments, key=lambda a: a.model_dump_json())
        }
        refused: Counter[EnumCapabilityInadmissibleReason] = Counter()
        # One verdict stands per item, source, model, backend and configuration:
        # the greatest by canonical content, so arrival order changes nothing.
        standing: dict[_RowKey, ModelCapabilityVerdictRow] = {}
        for row in dict.fromkeys(request.rows):
            reason = _admissibility(row, assignments)
            if reason is not None:
                refused[reason] += 1
                continue
            assert row.configuration is not None
            key = _row_key(row, row.configuration.configuration_id)
            held = standing.get(key)
            if held is None or row.model_dump_json() > held.model_dump_json():
                standing[key] = row

        by_cell: dict[_CellKey, list[ModelCapabilityVerdictRow]] = {}
        for key, row in standing.items():
            by_cell.setdefault(_cell_key(row, key[5]), []).append(row)

        cells = tuple(
            _build_cell(request.map_run_id, key, by_cell[key], request.line)
            for key in sorted(by_cell, key=str)
        )
        return ModelCapabilityMapFoldResult(
            map_run_id=request.map_run_id,
            cells=cells,
            best_configurations=best_configurations(cells),
            folded_row_count=len(standing),
            inadmissible=tuple(
                ModelCapabilityInadmissibleCount(reason=reason, count=count)
                for reason, count in sorted(refused.items())
            ),
        )


def _build_cell(
    map_run_id: str,
    key: _CellKey,
    members: Sequence[ModelCapabilityVerdictRow],
    line: ModelCapabilityLine,
) -> ModelCapabilityCell:
    tenant_id, model_key, backend_id, configuration_id, task_class, size_band = key
    n = len(members)
    passes = sum(1 for m in members if m.passed)
    low, high = _wilson_interval(passes, n)
    configuration = members[0].configuration
    assert configuration is not None
    bound = decision_lower_bound(passes=passes, n=n, confidence=line.confidence)
    required_n = required_sample_size(
        floor=line.floor,
        margin=line.margin,
        confidence=line.confidence,
        power=line.power,
    )
    if n < required_n:
        verdict = EnumCapabilityCellVerdict.REFUSED
    elif bound >= line.floor:
        verdict = EnumCapabilityCellVerdict.MET
    else:
        verdict = EnumCapabilityCellVerdict.MISSED
    return ModelCapabilityCell(
        tenant_id=tenant_id,
        map_run_id=map_run_id,
        model_key=model_key,
        backend_id=backend_id,
        configuration_id=configuration_id,
        configuration_parameters=configuration.parameters,
        task_class=task_class,
        size_band=size_band,
        n=n,
        passes=passes,
        pass_rate=passes / n,
        wilson_low=low,
        wilson_high=high,
        decision_lower_bound=bound,
        floor=line.floor,
        required_n=required_n,
        verdict=verdict,
    )


def best_configurations(
    cells: Iterable[ModelCapabilityCell],
) -> tuple[ModelCapabilityBestConfiguration, ...]:
    """The best and next-best configuration per model, backend, class and band.

    A deterministic read of the stored cells: MET cells only, ordered by decision
    lower bound, highest first, ties broken by configuration id. A group with no
    MET cell has no entry, so routing keeps the static tier order there.
    """
    groups: dict[
        tuple[UUID, str, str, str, str, EnumSizeBand], list[ModelCapabilityCell]
    ] = {}
    for cell in cells:
        if cell.verdict is not EnumCapabilityCellVerdict.MET:
            continue
        key = (
            cell.tenant_id,
            cell.map_run_id,
            cell.model_key,
            cell.backend_id,
            cell.task_class,
            cell.size_band,
        )
        groups.setdefault(key, []).append(cell)
    best: list[ModelCapabilityBestConfiguration] = []
    for key in sorted(groups, key=str):
        ranked = sorted(
            groups[key], key=lambda c: (-c.decision_lower_bound, c.configuration_id)
        )
        tenant_id, map_run_id, model_key, backend_id, task_class, size_band = key
        runner_up = ranked[1] if len(ranked) > 1 else None
        best.append(
            ModelCapabilityBestConfiguration(
                tenant_id=tenant_id,
                map_run_id=map_run_id,
                model_key=model_key,
                backend_id=backend_id,
                task_class=task_class,
                size_band=size_band,
                best_configuration_id=ranked[0].configuration_id,
                best_lower_bound=ranked[0].decision_lower_bound,
                next_best_configuration_id=(
                    runner_up.configuration_id if runner_up else None
                ),
                next_best_lower_bound=(
                    runner_up.decision_lower_bound if runner_up else None
                ),
            )
        )
    return tuple(best)
