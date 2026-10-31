# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Presentation sorting of an already selected projection window."""

from __future__ import annotations

from functools import cmp_to_key
from typing import Any

from omnimarket.projection.models import ProjectionOrderRank


def sort_for_presentation(
    rows: list[dict[str, Any]],
    order_by_spec: tuple[tuple[str, str, str | None], ...],
    order_rank: ProjectionOrderRank | None = None,
) -> list[dict[str, Any]]:
    """Sort an already-paged result for display without changing its cursor walk.

    Cursor pagination is always evaluated in ascending cursor order.  The
    contract's order is presentation-only and may be descending; applying it
    after the page is selected prevents a descending display from making the
    next-page predicate run backwards.

    A declared ``order_rank`` (OMN-17215 AC4) is the leading key, ahead of
    ``order_by_spec``. Every row's rank is resolved before sorting, so a value
    the rank does not declare raises :class:`UnrankedOrderValueError` rather
    than landing at an implicit position.
    """
    if not order_by_spec and order_rank is None:
        return rows

    # Resolve every row's rank before sorting, so an unranked value raises even
    # when the comparator would never have consulted that row.
    ranks: dict[int, int] = (
        {}
        if order_rank is None
        else {id(row): order_rank.rank_of(row.get(order_rank.column)) for row in rows}
    )

    def compare(left: dict[str, Any], right: dict[str, Any]) -> int:
        if order_rank is not None:
            rank_delta = ranks[id(left)] - ranks[id(right)]
            if rank_delta:
                return rank_delta
        for column, direction, nulls in order_by_spec:
            a, b = left.get(column), right.get(column)
            if a is None or b is None:
                if a is b:
                    continue
                nulls_first = nulls == "FIRST"
                # Preserve the cache's contract semantics: an omitted NULLS
                # clause keeps nulls last independent of ASC/DESC.
                result = (
                    (-1 if a is None else 1)
                    if nulls_first
                    else (1 if a is None else -1)
                )
                return result
            try:
                result = -1 if a < b else (1 if a > b else 0)
            except TypeError:
                sa, sb = str(a), str(b)
                result = -1 if sa < sb else (1 if sa > sb else 0)
            if result:
                return -result if direction == "DESC" else result
        return 0

    return sorted(rows, key=cmp_to_key(compare))
