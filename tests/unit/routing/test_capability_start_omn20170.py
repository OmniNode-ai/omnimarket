# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20170 (RS.6): routing starts at the cheapest tier measured MET for the band.

The capability map (RS.5) holds one cell per model, backend, configuration,
class and size band. For a request of class C and band B the chain starts at the
cheapest tier in the tier order that serves a backend whose (C, B) cell is MET.
A band with no MET cell, an unreadable map, and a class whose eval false-pass
line is not MET keep the static order. The routing log line names the class,
band, chosen cell, its n and its bound.
"""

from __future__ import annotations

import logging
from uuid import UUID

import pytest

from omnimarket.handlers.handler_capability_start import (
    REASON_BAND_CHEAPEST_MET,
    REASON_CLASS_LINE_NOT_MET,
    REASON_MAP_UNREADABLE,
    REASON_NO_MET_CELL,
    ModelCapabilityMapRead,
    ModelCapabilityStart,
    read_capability_cells,
    resolve_capability_start,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation.wire.model_attempt_configuration import (
    ModelAttemptConfiguration,
)
from omnimarket.models.delegation_capability_map.enum_capability_cell_verdict import (
    EnumCapabilityCellVerdict,
)
from omnimarket.models.delegation_capability_map.model_capability_cell import (
    ModelCapabilityCell,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    delta,
)
from tests.unit.delegation.test_roi_overlay_routing_omn14001 import (
    _request,
    code_gen_routable,
)

pytestmark = pytest.mark.unit

# Re-exported so pytest finds the shared three-tier routing fixture here.
__all__ = ["code_gen_routable"]

_TENANT = UUID("820272f9-4aaf-5add-a2df-0af942852ab2")
_CLASS = "code_generation"


def _cell(
    backend_id: str,
    *,
    verdict: EnumCapabilityCellVerdict = EnumCapabilityCellVerdict.MET,
    band: EnumSizeBand = EnumSizeBand.S,
    bound: float = 0.93,
    n: int = 120,
    configuration_id: str = "cfg-0000000000000001",
) -> ModelCapabilityCell:
    return ModelCapabilityCell(
        tenant_id=_TENANT,
        map_run_id="run-1",
        model_key="model",
        backend_id=backend_id,
        configuration_id=configuration_id,
        configuration_parameters=ModelAttemptConfiguration(),
        task_class=_CLASS,
        size_band=band,
        n=n,
        passes=n - 2,
        pass_rate=(n - 2) / n,
        wilson_low=0.9,
        wilson_high=0.99,
        decision_lower_bound=bound,
        floor=0.9,
        required_n=89,
        verdict=verdict,
    )


class _Tier:
    def __init__(self, name: str, *backends: str) -> None:
        self.name = name
        self.models = tuple(_Model(b) for b in backends)


class _Model:
    def __init__(self, backend_ref: str) -> None:
        self.backend_ref = backend_ref


_TIERS = (
    _Tier("local", "local-coder"),
    _Tier("cheap_frontier", "openrouter-north-mini-code"),
    _Tier("cheap_cloud", "cloud-gemini-2-5-flash"),
)


def _start(
    cells: tuple[ModelCapabilityCell, ...] | None, line: str | None = "met"
) -> ModelCapabilityStart:
    return resolve_capability_start(
        _TIERS,
        cells,
        task_class=_CLASS,
        size_band=EnumSizeBand.S,
        eval_line_verdict=line,
    )


# --- AC1: the chain starts at the cheapest MET tier for the class and band ------


def test_band_cheapest_met_starts_at_the_first_tier_with_a_met_cell() -> None:
    start = _start(
        (
            _cell("cloud-gemini-2-5-flash", bound=0.97),
            _cell("openrouter-north-mini-code", bound=0.92),
            _cell("local-coder", verdict=EnumCapabilityCellVerdict.MISSED),
        )
    )
    assert start.reason == REASON_BAND_CHEAPEST_MET
    # local's cell is MISSED, so the cheapest MET tier is cheap_frontier even
    # though cheap_cloud's bound is higher.
    assert start.start_tier_name == "cheap_frontier"
    assert start.cell is not None
    assert start.cell.backend_id == "openrouter-north-mini-code"


def test_band_cheapest_met_ignores_other_bands_and_classes() -> None:
    start = _start(
        (
            _cell("local-coder", band=EnumSizeBand.L),
            _cell("openrouter-north-mini-code").model_copy(
                update={"task_class": "test"}
            ),
            _cell("cloud-gemini-2-5-flash"),
        )
    )
    assert start.start_tier_name == "cheap_cloud"


def test_band_cheapest_met_picks_the_highest_bound_cell_of_the_tier() -> None:
    start = _start(
        (
            _cell("openrouter-north-mini-code", bound=0.91, configuration_id="cfg-a"),
            _cell("openrouter-north-mini-code", bound=0.95, configuration_id="cfg-b"),
        )
    )
    assert start.cell is not None
    assert start.cell.configuration_id == "cfg-b"


@pytest.mark.usefixtures("code_gen_routable")
def test_band_cheapest_met_routes_delta_to_the_measured_tier() -> None:
    static = delta(_request())
    assert static.tier_name == "local"
    measured = delta(
        _request(),
        capability_map=ModelCapabilityMapRead(
            cells=(_cell("openrouter-north-mini-code"),), eval_line_verdict="met"
        ),
    )
    assert measured.tier_name == "cheap_frontier"
    assert "Capability-start:" in measured.rationale


@pytest.mark.usefixtures("code_gen_routable")
def test_band_cheapest_met_leaves_an_escalation_floor_alone() -> None:
    decision = delta(
        _request(),
        min_tier_name="local",
        capability_map=ModelCapabilityMapRead(
            cells=(_cell("openrouter-north-mini-code"),), eval_line_verdict="met"
        ),
    )
    assert decision.tier_name == "local"
    assert "Capability-start:" not in decision.rationale


# --- AC2: no MET cell, unreadable map, or a class line not MET keep static ------


def test_band_no_met_static() -> None:
    start = _start(
        (
            _cell("local-coder", verdict=EnumCapabilityCellVerdict.REFUSED),
            _cell(
                "openrouter-north-mini-code", verdict=EnumCapabilityCellVerdict.MISSED
            ),
        )
    )
    assert (start.start_tier_name, start.reason) == (None, REASON_NO_MET_CELL)
    assert _start(()).reason == REASON_NO_MET_CELL


def test_map_unreadable_static() -> None:
    start = _start(None)
    assert (start.start_tier_name, start.reason) == (None, REASON_MAP_UNREADABLE)

    class _Broken:
        def read_cells(
            self, *, task_class: str, tenant_id: str
        ) -> tuple[ModelCapabilityCell, ...]:
            raise RuntimeError("relation delegation_capability_cells does not exist")

    assert (
        read_capability_cells(_Broken(), task_class=_CLASS, tenant_id=str(_TENANT))
        is None
    )
    assert (
        read_capability_cells(None, task_class=_CLASS, tenant_id=str(_TENANT)) is None
    )


@pytest.mark.parametrize("line", ["missed", "refused", "missing", "unreadable", None])
def test_class_line_not_met_static(line: str | None) -> None:
    start = _start((_cell("local-coder"),), line=line)
    assert (start.start_tier_name, start.reason) == (None, REASON_CLASS_LINE_NOT_MET)


@pytest.mark.usefixtures("code_gen_routable")
@pytest.mark.parametrize(
    "read",
    [
        ModelCapabilityMapRead(cells=None, eval_line_verdict="met"),
        ModelCapabilityMapRead(cells=(), eval_line_verdict="met"),
        ModelCapabilityMapRead(
            cells=(_cell("openrouter-north-mini-code"),), eval_line_verdict="missed"
        ),
    ],
)
def test_band_no_met_static_delta_keeps_the_static_order(
    read: ModelCapabilityMapRead,
) -> None:
    decision = delta(_request(), capability_map=read)
    assert decision.tier_name == "local"
    assert "Capability-start:" not in decision.rationale


# --- AC3: the routing log line names class, band, cell, n and bound -------------


@pytest.mark.usefixtures("code_gen_routable")
def test_band_routing_log_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    delta(
        _request(),
        capability_map=ModelCapabilityMapRead(
            cells=(_cell("openrouter-north-mini-code", n=133, bound=0.9312),),
            eval_line_verdict="met",
        ),
    )
    (line,) = [
        r.getMessage() for r in caplog.records if "capability start:" in r.getMessage()
    ]
    for part in (
        f"class={_CLASS}",
        "band=S",
        "tier=cheap_frontier",
        "cell=model/openrouter-north-mini-code/cfg-0000000000000001",
        "n=133",
        "bound=0.9312",
        f"reason={REASON_BAND_CHEAPEST_MET}",
    ):
        assert part in line


def test_band_routing_log_line_names_the_static_reason() -> None:
    line = _start(None).describe()
    assert "class=code_generation" in line
    assert "band=S" in line
    assert "cell=none" in line
    assert f"reason={REASON_MAP_UNREADABLE}" in line
