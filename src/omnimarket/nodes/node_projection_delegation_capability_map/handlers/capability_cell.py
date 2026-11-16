# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One capability cell from its member rows and the class line (OMN-20169)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from uuid import UUID

from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation_capability_map.enum_capability_cell_verdict import (
    EnumCapabilityCellVerdict,
)
from omnimarket.models.delegation_capability_map.model_capability_cell import (
    ModelCapabilityCell,
)
from omnimarket.models.delegation_capability_map.model_capability_line import (
    ModelCapabilityLine,
)
from omnimarket.models.delegation_capability_map.model_capability_verdict_row import (
    ModelCapabilityVerdictRow,
)
from omnimarket.ranges.power import decision_lower_bound, required_sample_size

_Z = 1.959963984540054

_CellKey = tuple[UUID, str, str, str, str, EnumSizeBand]


def _wilson_interval(passes: int, n: int) -> tuple[float, float]:
    """Two-sided Wilson 95 percent interval for a pass proportion."""
    p = passes / n
    denominator = 1 + _Z**2 / n
    center = (p + _Z**2 / (2 * n)) / denominator
    half = _Z * math.sqrt(p * (1 - p) / n + _Z**2 / (4 * n**2)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def build_cell(
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
