# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parse a ROUTE line against the routing contract's data (OMN-22263).

The marker, the dimension names, the digit range and the band edges all come from
the routing contract the caller loaded; this module holds only the line grammar.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import yaml

from omnimarket.models.work_classification.model_work_classification import (
    ModelRouteDimensions,
    ModelRouteLine,
)
from omnimarket.nodes.node_route_line_parse_compute.models import (
    EnumRouteParseStatus,
    ModelRouteLineParseRequest,
    ModelRouteLineParseResult,
    ModelRoutingContract,
)


def load_routing_contract(text: str) -> ModelRoutingContract:
    """Read the routing contract's YAML; a contract with no dimensions or bands fails validation naming the field."""
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise ValueError("routing contract is not a mapping")
    return ModelRoutingContract.model_validate(document)


def _line_pattern(contract: ModelRoutingContract) -> re.Pattern[str]:
    bands = "|".join(
        re.escape(band.name)
        for band in sorted(contract.bands, key=lambda b: len(b.name), reverse=True)
    )
    dims = "".join(
        f"{re.escape(name)}(?P<d{index}>[0-{contract.dimension_max}])"
        for index, name in enumerate(contract.dimensions)
    )
    return re.compile(
        rf"{re.escape(contract.marker)} band=(?P<band>{bands}) score=(?P<score>\d+) "
        rf"dims={dims} dest=(?P<dest>[^\s\\]+) model=(?P<model>[A-Za-z0-9_.-]+) "
        r"effort=(?P<effort>[A-Za-z0-9_.-]+)"
    )


def _refused(field: str, reason: str) -> ModelRouteLineParseResult:
    return ModelRouteLineParseResult(
        status=EnumRouteParseStatus.REFUSED,
        route=None,
        refused_field=field,
        reason=reason,
    )


def _parse_at(
    contract: ModelRoutingContract, text: str, start: int
) -> ModelRouteLineParseResult:
    """Parse the ROUTE line that begins at ``start``; a refusal names the failing field."""
    match = _line_pattern(contract).match(text, start)
    if match is None:
        return _refused("line", "the line after the marker does not match the grammar")
    values = tuple(
        (name, int(match[f"d{index}"]))
        for index, name in enumerate(contract.dimensions)
    )
    score = int(match["score"])
    total = sum(value for _, value in values)
    if score != total:
        return _refused(
            "score", f"score={score} is not the sum of its dimensions ({total})"
        )
    by_name = {band.name: band for band in contract.bands}
    band = by_name[match["band"]]
    implied = [
        candidate.rank
        for candidate in contract.bands
        if candidate.score_min is not None
        and candidate.score_max is not None
        and candidate.score_min <= score <= candidate.score_max
    ]
    if implied and band.rank < max(implied):
        return _refused(
            "band", f"band {band.name} is below the band that score {score} implies"
        )
    return ModelRouteLineParseResult(
        status=EnumRouteParseStatus.PARSED,
        route=ModelRouteLine(
            band=band.name,
            score=score,
            dimensions=ModelRouteDimensions(values=values),
            dest=match["dest"],
            model=match["model"],
            effort=match["effort"],
        ),
        refused_field=None,
        reason=None,
    )


def _marker_offsets(text: str, marker: str) -> Iterator[int]:
    start = text.find(marker)
    while start != -1:
        yield start
        start = text.find(marker, start + len(marker))


class HandlerRouteLineParse:
    """Pure compute: text and routing contract in, a parsed ROUTE line or a named refusal out.

    A brief may mention the marker before its real line (the format quoted as prose or
    as a template), so every marker occurrence is tried in order. The first occurrence
    that validates is the route; when none validates, the refusal of the first
    occurrence is returned, so a lone malformed line is still refused naming its field.
    """

    def handle(self, request: ModelRouteLineParseRequest) -> ModelRouteLineParseResult:
        contract = request.routing_contract
        first_refusal: ModelRouteLineParseResult | None = None
        for start in _marker_offsets(request.text, contract.marker):
            result = _parse_at(contract, request.text, start)
            if result.status is EnumRouteParseStatus.PARSED:
                return result
            if first_refusal is None:
                first_refusal = result
        if first_refusal is not None:
            return first_refusal
        return ModelRouteLineParseResult(
            status=EnumRouteParseStatus.ABSENT,
            route=None,
            refused_field=None,
            reason=None,
        )


__all__ = ["HandlerRouteLineParse", "load_routing_contract"]
