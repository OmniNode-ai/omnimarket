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


TEMPLATE = (
    "ROUTE: band=<B> score=<n> dims=S<s>A<a>R<r>V<v>N<n>C<c>J<j> "
    "dest=<d> model=<m> effort=<e>"
)


def test_a_brief_quoting_the_format_as_a_template_before_its_real_line_parses_the_real_line() -> (
    None
):
    real_alone = _parse(GOOD)
    assert real_alone.status is EnumRouteParseStatus.PARSED
    result = _parse(
        f"Put a line in this form at the end of the brief:\n{TEMPLATE}\n\n{GOOD}\n"
    )
    assert result.status is EnumRouteParseStatus.PARSED
    assert result.route == real_alone.route
    assert result.refused_field is None


def test_a_brief_quoting_the_marker_in_prose_before_its_real_line_parses_the_real_line() -> (
    None
):
    result = _parse(
        f"Every brief ends with a ROUTE: line, and this one does too.\n{GOOD}\n"
    )
    assert result.status is EnumRouteParseStatus.PARSED
    assert result.route is not None
    assert result.route.band == "B3"


def test_with_two_markers_where_only_the_later_one_is_valid_the_later_one_parses() -> (
    None
):
    bad_score = GOOD.replace("score=13", "score=12")
    result = _parse(f"{bad_score}\nsecond try:\n{GOOD}")
    assert result.status is EnumRouteParseStatus.PARSED
    assert result.route is not None
    assert result.route.score == 13


def test_with_several_valid_markers_the_first_valid_one_wins() -> None:
    other = GOOD.replace("band=B3", "band=B4").replace("score=13", "score=14")
    other = other.replace("S2A2R1V1N2C3J2", "S2A2R1V1N2C3J3")
    first_is_other = _parse(f"{other}\n{GOOD}")
    assert first_is_other.route is not None
    assert first_is_other.route.band == "B4"
    first_is_good = _parse(f"{GOOD}\n{other}")
    assert first_is_good.route is not None
    assert first_is_good.route.band == "B3"


def test_when_no_marker_validates_the_first_markers_refusal_is_reported() -> None:
    bad_score = GOOD.replace("score=13", "score=12")
    result = _parse(f"{bad_score}\nthen prose with a ROUTE: marker and nothing else")
    assert result.status is EnumRouteParseStatus.REFUSED
    assert result.route is None
    assert result.refused_field == "score"
