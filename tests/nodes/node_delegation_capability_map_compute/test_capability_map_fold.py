# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20169 (RS.5): the capability map fold, its admissibility rule and its reads."""

from __future__ import annotations

import random
from uuid import UUID

import pytest

from omnimarket.enums.enum_delegation_context_strategy import (
    EnumDelegationContextStrategy,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation.wire.model_attempt_configuration import (
    ModelAttemptConfiguration,
)
from omnimarket.models.delegation_capability_map.enum_capability_cell_verdict import (
    EnumCapabilityCellVerdict,
)
from omnimarket.models.delegation_capability_map.enum_capability_inadmissible_reason import (
    EnumCapabilityInadmissibleReason,
)
from omnimarket.models.delegation_capability_map.enum_capability_source import (
    EnumCapabilitySource,
)
from omnimarket.models.delegation_capability_map.model_capability_configuration import (
    ModelCapabilityConfiguration,
)
from omnimarket.models.delegation_capability_map.model_capability_grid import (
    ModelCapabilityGridItem,
    ModelCapabilityGridModel,
    ModelCapabilityGridRequest,
)
from omnimarket.models.delegation_capability_map.model_capability_identity import (
    ModelCapabilityIdentity,
)
from omnimarket.models.delegation_capability_map.model_capability_map_fold import (
    ModelCapabilityMapFoldRequest,
)
from omnimarket.models.delegation_capability_map.model_capability_serving_assignment import (
    ModelCapabilityServingAssignment,
)
from omnimarket.models.delegation_capability_map.model_capability_verdict_row import (
    ModelCapabilityVerdictRow,
)
from omnimarket.nodes.node_projection_delegation_capability_map.handlers import (
    HandlerCapabilityGridRun,
    HandlerDelegationCapabilityMap,
    best_configurations,
)
from omnimarket.ranges.power import decision_lower_bound, required_sample_size

pytestmark = pytest.mark.unit

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_DEFAULT = ModelCapabilityConfiguration()
_HOT = ModelCapabilityConfiguration(
    parameters=ModelAttemptConfiguration(temperature=0.7)
)
_FACTS = ModelCapabilityConfiguration(
    parameters=ModelAttemptConfiguration(
        context_strategy_id=EnumDelegationContextStrategy.COMPUTED_FACTS
    )
)
_REQUIRED_N = required_sample_size(floor=0.90, margin=0.07, confidence=0.95, power=0.8)


def _identity(
    configuration: ModelCapabilityConfiguration, **overrides: str | None
) -> ModelCapabilityIdentity:
    fields: dict[str, str | None] = {
        "model_revision": "qwen3-coder-30b@2026-09",
        "backend": "local-url",
        "inference_configuration": configuration.configuration_id,
        "suite_revision": "ev4-blind-v1",
        "grader_revision": "blind_model:claude-opus-5-5",
        "harness_revision": "delegation_eval_items",
    }
    fields.update(overrides)
    return ModelCapabilityIdentity(**fields)


def _assignment(
    configuration: ModelCapabilityConfiguration,
) -> ModelCapabilityServingAssignment:
    return ModelCapabilityServingAssignment(
        model_key="qwen3-coder-30b",
        backend_id="local-url",
        configuration_id=configuration.configuration_id,
        identity=_identity(configuration),
    )


def _rows(
    n: int,
    passes: int,
    *,
    configuration: ModelCapabilityConfiguration | None = _DEFAULT,
    band: EnumSizeBand = EnumSizeBand.S,
    task_class: str = "code_review",
    identity: ModelCapabilityIdentity | None = None,
    prefix: str = "item",
) -> list[ModelCapabilityVerdictRow]:
    stamped = identity or _identity(configuration or _DEFAULT)
    return [
        ModelCapabilityVerdictRow(
            tenant_id=_TENANT,
            item_key=f"{prefix}-{task_class}-{band.value}-{i}",
            source=EnumCapabilitySource.LABELLED_ITEM,
            task_class=task_class,
            size_band=band,
            model_key="qwen3-coder-30b",
            backend_id="local-url",
            configuration=configuration,
            identity=stamped,
            passed=i < passes,
        )
        for i in range(n)
    ]


def _fold(
    rows: list[ModelCapabilityVerdictRow],
    configurations: tuple[ModelCapabilityConfiguration, ...] = (_DEFAULT,),
):
    return HandlerDelegationCapabilityMap().handle(
        ModelCapabilityMapFoldRequest(
            map_run_id="run-1",
            rows=tuple(rows),
            assignments=tuple(_assignment(c) for c in configurations),
        )
    )


def test_one_cell_per_model_backend_class_and_band_with_n_passes_wilson_and_bound() -> (
    None
):
    rows = _rows(_REQUIRED_N + 11, _REQUIRED_N + 10) + _rows(12, 6, band=EnumSizeBand.M)
    result = _fold(rows)
    cells = {(c.task_class, c.size_band): c for c in result.cells}
    assert set(cells) == {
        ("code_review", EnumSizeBand.S),
        ("code_review", EnumSizeBand.M),
    }
    small = cells["code_review", EnumSizeBand.S]
    assert (small.model_key, small.backend_id) == ("qwen3-coder-30b", "local-url")
    assert (small.n, small.passes) == (_REQUIRED_N + 11, _REQUIRED_N + 10)
    assert small.wilson_low < small.pass_rate < small.wilson_high
    assert small.decision_lower_bound == decision_lower_bound(
        passes=small.passes, n=small.n, confidence=0.95
    )
    assert small.required_n == _REQUIRED_N
    assert small.verdict is EnumCapabilityCellVerdict.MET
    assert result.folded_row_count == len(rows)


def test_the_fold_is_order_and_redelivery_independent() -> None:
    rows = _rows(40, 30) + _rows(30, 10, band=EnumSizeBand.L)
    shuffled = list(rows) + rows[:7]
    random.Random(7).shuffle(shuffled)
    assert _fold(rows) == _fold(shuffled)


def test_underpowered_refused_below_the_required_sample() -> None:
    (cell,) = _fold(_rows(_REQUIRED_N - 1, _REQUIRED_N - 1)).cells
    assert cell.n == _REQUIRED_N - 1
    assert cell.passes == cell.n
    assert cell.verdict is EnumCapabilityCellVerdict.REFUSED
    (met,) = _fold(_rows(_REQUIRED_N, _REQUIRED_N)).cells
    assert met.verdict is EnumCapabilityCellVerdict.MET


def test_absent_band_not_zero() -> None:
    result = _fold(_rows(20, 20, band=EnumSizeBand.S))
    bands = {cell.size_band for cell in result.cells}
    assert bands == {EnumSizeBand.S}
    assert all(cell.n >= 1 for cell in result.cells)
    assert _fold([]).cells == ()


@pytest.mark.parametrize(
    "field",
    [
        "model_revision",
        "backend",
        "inference_configuration",
        "suite_revision",
        "grader_revision",
        "harness_revision",
    ],
)
def test_identity_mismatch_inadmissible(field: str) -> None:
    admitted = _rows(10, 10, prefix="ok")
    swapped = _rows(
        10,
        0,
        identity=_identity(_DEFAULT, **{field: "something-else"}),
        prefix="swapped",
    )
    result = _fold(admitted + swapped)
    (cell,) = result.cells
    assert (cell.n, cell.passes) == (10, 10)
    assert [(e.reason, e.count) for e in result.inadmissible] == [
        (EnumCapabilityInadmissibleReason.IDENTITY_MISMATCH, 10)
    ]


def test_identity_mismatch_inadmissible_when_a_field_is_missing_or_unassigned() -> None:
    missing = _rows(5, 5, identity=_identity(_DEFAULT, harness_revision=None))
    unassigned = _rows(4, 4, configuration=_HOT, prefix="hot")
    result = _fold(missing + unassigned)
    assert result.cells == ()
    assert {e.reason: e.count for e in result.inadmissible} == {
        EnumCapabilityInadmissibleReason.IDENTITY_FIELD_MISSING: 5,
        EnumCapabilityInadmissibleReason.NO_SERVING_ASSIGNMENT: 4,
    }


def test_configuration_in_cell_key() -> None:
    rows = (
        _rows(30, 30, configuration=_DEFAULT, prefix="a")
        + _rows(30, 3, configuration=_HOT, prefix="b")
        + _rows(6, 6, configuration=None, prefix="c")
    )
    result = _fold(rows, configurations=(_DEFAULT, _HOT))
    by_configuration = {c.configuration_id: c for c in result.cells}
    assert set(by_configuration) == {
        _DEFAULT.configuration_id,
        _HOT.configuration_id,
    }
    assert by_configuration[_DEFAULT.configuration_id].passes == 30
    assert by_configuration[_HOT.configuration_id].passes == 3
    assert (
        by_configuration[_HOT.configuration_id].configuration_parameters
        == _HOT.parameters
    )
    assert {e.reason: e.count for e in result.inadmissible} == {
        EnumCapabilityInadmissibleReason.NO_CONFIGURATION: 6
    }
    assert _DEFAULT.configuration_id != _HOT.configuration_id
    assert ModelCapabilityConfiguration().configuration_id == _DEFAULT.configuration_id


def _grid_request(n_items: int) -> ModelCapabilityGridRequest:
    return ModelCapabilityGridRequest(
        tenant_id=_TENANT,
        items=tuple(
            ModelCapabilityGridItem(
                item_key=f"grid-{i}",
                task_class="code_review",
                size_band=EnumSizeBand.S,
                prompt=f"review hunk {i}",
            )
            for i in range(n_items)
        ),
        models=(
            ModelCapabilityGridModel(
                model_key="qwen3-coder-30b",
                backend_id="local-url",
                model_revision="qwen3-coder-30b@2026-09",
            ),
        ),
        configurations=(_DEFAULT, _HOT, _FACTS),
        suite_revision="ev4-blind-v1",
        grader_revision="blind_model:claude-opus-5-5",
        harness_revision="delegation_eval_items",
    )


def _grader(fails_per_configuration: dict[str, int]):
    def grade(item, model, configuration) -> bool:
        del model
        index = int(item.item_key.split("-")[1])
        return index >= fails_per_configuration[configuration.configuration_id]

    return grade


def test_best_configuration_deterministic_from_an_offline_grid_run() -> None:
    request = _grid_request(100)
    fails = {
        _DEFAULT.configuration_id: 2,
        _HOT.configuration_id: 0,
        _FACTS.configuration_id: 30,
    }
    rows = HandlerCapabilityGridRun(_grader(fails)).handle(request)
    # One verdict row per (item, model, configuration).
    assert len(rows) == 100 * 1 * 3
    keys = {
        (r.item_key, r.model_key, r.configuration.configuration_id)
        for r in rows
        if r.configuration is not None
    }
    assert len(keys) == 300
    assert {r.source for r in rows} == {EnumCapabilitySource.GRID_RUN}

    result = _fold(list(rows), configurations=(_DEFAULT, _HOT, _FACTS))
    verdicts = {c.configuration_id: c.verdict for c in result.cells}
    assert verdicts[_FACTS.configuration_id] is EnumCapabilityCellVerdict.MISSED
    (best,) = result.best_configurations
    assert best.best_configuration_id == _HOT.configuration_id
    assert best.next_best_configuration_id == _DEFAULT.configuration_id
    assert best.best_lower_bound > (best.next_best_lower_bound or 0.0)
    # A deterministic read of the stored cells: same answer in any order.
    assert best_configurations(reversed(result.cells)) == result.best_configurations
    shuffled = list(rows)
    random.Random(3).shuffle(shuffled)
    assert _fold(shuffled, configurations=(_DEFAULT, _HOT, _FACTS)) == result


def test_best_configuration_deterministic_tie_breaks_by_configuration_id() -> None:
    fails = {c.configuration_id: 0 for c in (_DEFAULT, _HOT, _FACTS)}
    rows = HandlerCapabilityGridRun(_grader(fails)).handle(_grid_request(100))
    (best,) = _fold(
        list(rows), configurations=(_DEFAULT, _HOT, _FACTS)
    ).best_configurations
    ordered = sorted(c.configuration_id for c in (_DEFAULT, _HOT, _FACTS))
    assert (best.best_configuration_id, best.next_best_configuration_id) == (
        ordered[0],
        ordered[1],
    )


def test_no_met_no_best() -> None:
    fails = {c.configuration_id: 40 for c in (_DEFAULT, _HOT, _FACTS)}
    rows = HandlerCapabilityGridRun(_grader(fails)).handle(_grid_request(100))
    result = _fold(list(rows), configurations=(_DEFAULT, _HOT, _FACTS))
    assert result.cells
    assert all(c.verdict is not EnumCapabilityCellVerdict.MET for c in result.cells)
    assert result.best_configurations == ()
    underpowered = _fold(_rows(10, 10))
    assert underpowered.best_configurations == ()


def test_no_met_no_best_single_met_configuration_has_no_next_best() -> None:
    fails = {
        _DEFAULT.configuration_id: 0,
        _HOT.configuration_id: 40,
        _FACTS.configuration_id: 40,
    }
    rows = HandlerCapabilityGridRun(_grader(fails)).handle(_grid_request(100))
    (best,) = _fold(
        list(rows), configurations=(_DEFAULT, _HOT, _FACTS)
    ).best_configurations
    assert best.best_configuration_id == _DEFAULT.configuration_id
    assert best.next_best_configuration_id is None


def test_a_grid_holds_at_most_eight_distinct_configurations() -> None:
    nine = tuple(
        ModelCapabilityConfiguration(
            parameters=ModelAttemptConfiguration(temperature=t / 10)
        )
        for t in range(9)
    )
    with pytest.raises(ValueError, match="at most 8"):
        ModelCapabilityGridRequest.model_validate(
            _grid_request(1).model_dump()
            | {"configurations": [c.model_dump() for c in nine]}
        )
    with pytest.raises(ValueError, match="distinct"):
        ModelCapabilityGridRequest.model_validate(
            _grid_request(1).model_dump()
            | {"configurations": [_DEFAULT.model_dump(), _DEFAULT.model_dump()]}
        )


def test_a_verdict_row_round_trips_through_json() -> None:
    (row,) = _rows(1, 1, configuration=_HOT)
    again = ModelCapabilityVerdictRow.model_validate_json(row.model_dump_json())
    assert again == row
    assert again.configuration is not None
    assert again.configuration.configuration_id == _HOT.configuration_id


def test_the_fold_makes_no_model_call_and_reads_no_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket
    import time

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the capability map fold is definition B")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(time, "time", refuse)
    assert _fold(_rows(5, 4)).cells
