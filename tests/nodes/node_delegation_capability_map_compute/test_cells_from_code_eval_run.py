# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23628: an eval run carries its measured size band and class line.

The capability cells an offline code-eval run produces are now judged against
the class line read from the deployment overlay, and every verdict row keeps
the size band its observation measured. One cell therefore stands per band and
budget, and the measured band reaches the routing start.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.handlers.handler_capability_line_overlay import (
    resolve_capability_line,
)
from omnimarket.handlers.handler_capability_start import (
    REASON_NO_MET_CELL,
    resolve_capability_start,
)
from omnimarket.handlers.handler_harness_eval import (
    ModelHarnessObservation,
    build_harness_capability_run,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation_capability_map.enum_capability_cell_verdict import (
    EnumCapabilityCellVerdict,
)
from omnimarket.models.delegation_capability_map.model_capability_line import (
    ModelCapabilityLine,
)
from omnimarket.models.delegation_capability_map.model_capability_map_fold import (
    ModelCapabilityMapFoldRequest,
)
from omnimarket.models.delegation_capability_map.model_capability_map_run_recorded import (
    ModelCapabilityMapRunRecorded,
)
from omnimarket.nodes.node_projection_delegation_capability_map.handlers import (
    HandlerDelegationCapabilityMap,
)

pytestmark = pytest.mark.unit

_TENANT = UUID("33333333-3333-3333-3333-333333333333")
_OBSERVED_AT = datetime(2026, 11, 11, 9, 0, 0, tzinfo=UTC)
_HARNESS = "local-agent"
_MODEL = "qwen-test"
_TASK_CLASS = "code-edit"
_BUDGETS = ("turns12-600s", "turns30-1200s")
# 330 trials per (band, budget) clears the required n at both floors used below (321 at 0.50).
_PASSES_PER_BAND = {EnumSizeBand.S: 287, EnumSizeBand.M: 250}
_N_PER_CELL = 330


class _TierModel:
    def __init__(self, backend_ref: str) -> None:
        self.backend_ref = backend_ref


class _Tier:
    def __init__(self, name: str, backends: tuple[str, ...]) -> None:
        self.name = name
        self.models = tuple(_TierModel(backend) for backend in backends)


def _write_overlay(tmp_path, floor: float) -> dict[str, str]:
    """A capability-map overlay for the node, pinned to ``floor`` under ``tmp_path``."""
    node_dir = tmp_path / "node_projection_delegation_capability_map"
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "overlay.yaml").write_text(
        f"line:\n  floor: {floor}\n  margin: 0.07\n  confidence: 0.95\n  power: 0.8\n",
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def _observations() -> tuple[ModelHarnessObservation, ...]:
    # 330 labelled trials per (band, budget): enough for a cell to be judged against the line.
    rows: list[ModelHarnessObservation] = []
    for band in (EnumSizeBand.S, EnumSizeBand.M):
        for budget in _BUDGETS:
            for i in range(_N_PER_CELL):
                passed = i < _PASSES_PER_BAND[band]
                rows.append(
                    ModelHarnessObservation(
                        item_key=f"{band.value}-{budget}-{i}",
                        task_class=_TASK_CLASS,
                        harness=_HARNESS,
                        model=_MODEL,
                        configuration=budget,
                        duration_s=10,
                        passed=passed,
                        harness_succeeded=passed,
                        available=True,
                        size_band=band,
                    )
                )
    return tuple(rows)


def _run_and_result(observations: tuple[ModelHarnessObservation, ...], line) -> tuple:
    """The lab path: build the map run from the line, then fold it into cells."""
    run = build_harness_capability_run(
        observations,
        tenant_id=_TENANT,
        observed_at=_OBSERVED_AT,
        suite_revision="suite-1",
        harness_revision="h-1",
        line=line,
    )
    return run, HandlerDelegationCapabilityMap().handle(run.fold_request())


def cells_for(observations: tuple[ModelHarnessObservation, ...], environ) -> tuple:
    """Read the class line from the overlay, then run and fold the observations."""
    return _run_and_result(observations, resolve_capability_line(environ))


def _recount(
    observations: tuple[ModelHarnessObservation, ...], size_band, configuration: str
) -> tuple[int, int]:
    """The (n, passes) an available observation group of a band and budget carries."""
    group = [
        o
        for o in observations
        if o.size_band is size_band and o.configuration == configuration and o.available
    ]
    return len(group), sum(1 for o in group if o.passed)


def test_one_cell_per_band_and_budget(tmp_path) -> None:
    observations = _observations()
    environ = _write_overlay(tmp_path, 0.50)
    _, result = cells_for(observations, environ)
    assert len(result.cells) == 4
    assert {(c.size_band, c.backend_id) for c in result.cells} == {
        (EnumSizeBand.S, f"harness:{_HARNESS}:turns12-600s"),
        (EnumSizeBand.S, f"harness:{_HARNESS}:turns30-1200s"),
        (EnumSizeBand.M, f"harness:{_HARNESS}:turns12-600s"),
        (EnumSizeBand.M, f"harness:{_HARNESS}:turns30-1200s"),
    }
    for cell in result.cells:
        configuration = cell.backend_id.split(":")[2]
        n, passes = _recount(observations, cell.size_band, configuration)
        assert cell.n == n == _N_PER_CELL
        assert cell.passes == passes


def test_size_band_is_read_from_the_observation(tmp_path) -> None:
    observations = _observations()
    environ = _write_overlay(tmp_path, 0.50)
    run, _ = cells_for(observations, environ)
    assert Counter(row.size_band for row in run.rows) == Counter(
        o.size_band for o in observations
    )


def test_eval_cells_start_no_tier(tmp_path) -> None:
    _, result = cells_for(_observations(), _write_overlay(tmp_path, 0.50))
    # Non-vacuous: every eval cell reads MET, so a missing start is a backend gap.
    assert all(c.verdict is EnumCapabilityCellVerdict.MET for c in result.cells)
    tiers = (_Tier("tier-0", ("local-coder",)), _Tier("tier-1", ("cloud-glm",)))
    start = resolve_capability_start(
        tiers,
        result.cells,
        task_class=_TASK_CLASS,
        size_band=EnumSizeBand.S,
        eval_line_verdict="met",
    )
    assert start.start_tier_name is None
    assert start.reason == REASON_NO_MET_CELL
    # Positive control: a MET cell served by the first tier's backend starts there.
    met = next(c for c in result.cells if c.size_band is EnumSizeBand.S)
    started = resolve_capability_start(
        tiers,
        (*result.cells, met.model_copy(update={"backend_id": "local-coder"})),
        task_class=_TASK_CLASS,
        size_band=EnumSizeBand.S,
        eval_line_verdict="met",
    )
    assert started.start_tier_name == "tier-0"


def assert_line_follows_overlay(read_line, tmp_path) -> None:
    """The read line and its cells follow the overlay floor, and the verdicts differ.

    ``read_line`` is the callable that turns an overlay environ into the class
    line, so a broken reader is pinned by the same assertions.
    """
    verdicts: dict[float, EnumCapabilityCellVerdict] = {}
    for floor in (0.50, 0.90):
        environ = _write_overlay(tmp_path, floor)
        line = read_line(environ)
        assert line.floor == floor
        run, result = _run_and_result(_observations(), line)
        assert run.line.floor == floor
        cell = next(
            c
            for c in result.cells
            if c.size_band is EnumSizeBand.S
            and c.backend_id == f"harness:{_HARNESS}:turns12-600s"
        )
        verdicts[floor] = cell.verdict
    assert verdicts[0.50] is not verdicts[0.90]


def test_overlay_floor_changes_the_line_and_the_verdict(tmp_path) -> None:
    assert_line_follows_overlay(resolve_capability_line, tmp_path)


def _broken_line_reader(environ) -> ModelCapabilityLine:
    # The break: read the line from the model's defaults, ignoring the overlay.
    return ModelCapabilityLine(floor=0.90, margin=0.07, confidence=0.95, power=0.8)


def test_failing_control_read_line_from_defaults(tmp_path) -> None:
    """Break under test: a reader that returns the model's default line (floor 0.90)
    and ignores the overlay, so both floors read the same line and the verdicts
    no longer differ."""
    with pytest.raises(AssertionError):
        assert_line_follows_overlay(_broken_line_reader, tmp_path)
    assert_line_follows_overlay(resolve_capability_line, tmp_path)


def test_no_overlay_means_no_line() -> None:
    with pytest.raises(NodeOverlayError):
        resolve_capability_line(environ={})


def test_line_has_no_default() -> None:
    with pytest.raises(ValidationError):
        ModelCapabilityLine()
    with pytest.raises(ValidationError):
        ModelCapabilityMapFoldRequest(map_run_id="x")
    with pytest.raises(ValidationError):
        ModelCapabilityMapRunRecorded(
            tenant_id=_TENANT, map_run_id="x", observed_at=_OBSERVED_AT
        )


def test_observation_requires_size_band() -> None:
    with pytest.raises(ValidationError):
        ModelHarnessObservation(
            item_key="a",
            task_class=_TASK_CLASS,
            harness=_HARNESS,
            model=_MODEL,
            configuration="turns12-600s",
            duration_s=10,
            passed=True,
            harness_succeeded=True,
        )
