# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 and AC10: the ROUTE line parse, its refusals and the absent line (OMN-22263)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_route_line_parse_compute.handlers.handler_route_line_parse import (
    HandlerRouteLineParse,
    load_routing_contract,
)
from omnimarket.nodes.node_route_line_parse_compute.models import (
    EnumRouteParseStatus,
    ModelRouteLineParseRequest,
    ModelRouteLineParseResult,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "routing_contract.v1.yaml"
GOOD = (
    "ROUTE: band=B3 score=13 dims=S2A2R1V1N2C3J2 dest=claude model=sonnet effort=high"
)


def _parse(text: str) -> ModelRouteLineParseResult:
    contract = load_routing_contract(FIXTURE.read_text(encoding="utf-8"))
    return HandlerRouteLineParse().handle(
        ModelRouteLineParseRequest(text=text, routing_contract=contract)
    )


def test_a_good_line_returns_band_score_and_seven_dimensions_summing_to_the_score() -> (
    None
):
    result = _parse(f"brief text\n{GOOD}\nmore text")
    assert result.status is EnumRouteParseStatus.PARSED
    route = result.route
    assert route is not None
    assert route.band == "B3"
    assert route.score == 13
    assert dict(route.dimensions.values) == {
        "S": 2,
        "A": 2,
        "R": 1,
        "V": 1,
        "N": 2,
        "C": 3,
        "J": 2,
    }
    assert sum(value for _, value in route.dimensions.values) == route.score
    assert (route.dest, route.model, route.effort) == ("claude", "sonnet", "high")


def test_a_score_that_differs_from_the_sum_is_refused_naming_the_score_field() -> None:
    result = _parse(GOOD.replace("score=13", "score=12"))
    assert result.status is EnumRouteParseStatus.REFUSED
    assert result.route is None
    assert result.refused_field == "score"
    assert "12" in (result.reason or "")


def test_a_band_below_the_band_the_score_implies_is_refused_naming_the_band_field() -> (
    None
):
    result = _parse(GOOD.replace("band=B3", "band=B1"))
    assert result.status is EnumRouteParseStatus.REFUSED
    assert result.refused_field == "band"


def test_a_malformed_line_is_refused_not_guessed() -> None:
    result = _parse("ROUTE: band=B3 score=13 dims=S2 dest=claude")
    assert result.status is EnumRouteParseStatus.REFUSED
    assert result.route is None
    assert result.refused_field == "line"


def test_a_unit_without_a_route_line_carries_route_none_and_no_default_band() -> None:
    result = _parse("a brief with no routing line at all")
    assert result.status is EnumRouteParseStatus.ABSENT
    assert result.route is None
    assert result.refused_field is None


def test_the_dimension_names_come_from_the_contract_data() -> None:
    contract = load_routing_contract(
        FIXTURE.read_text(encoding="utf-8").replace(
            "dimensions: [S, A, R, V, N, C, J]", "dimensions: [S, A]"
        )
    )
    result = HandlerRouteLineParse().handle(
        ModelRouteLineParseRequest(
            text="ROUTE: band=B2 score=5 dims=S2A3 dest=claude model=sonnet effort=high",
            routing_contract=contract,
        )
    )
    assert result.route is not None
    assert dict(result.route.dimensions.values) == {"S": 2, "A": 3}


def test_a_contract_without_dimensions_is_refused_at_load() -> None:
    with pytest.raises(ValueError, match="dimensions"):
        load_routing_contract(
            FIXTURE.read_text(encoding="utf-8").replace(
                "dimensions: [S, A, R, V, N, C, J]", "dimensions: []"
            )
        )
