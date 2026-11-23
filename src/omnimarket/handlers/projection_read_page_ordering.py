# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ordering of one projection page, split out of read_page."""

from __future__ import annotations

from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionOrderRank, ProjectionTableConfig


class InvalidOrderByError(ValueError):
    """A request-time ``order_by`` query value failed to parse or validate.

    Distinct from :class:`~omnimarket.projection.discovery.MalformedOrderBySpecError`
    (OMN-16290): that one is a contract-authoring defect and hard-fails
    startup; this one is untrusted caller input on a live request and must
    map to a ``422``, never crash the process or silently fall back to the
    contract default (no-defensive-defaults).
    """


def base_order_by_spec(
    cfg: ProjectionTableConfig, order_by: str | None
) -> tuple[tuple[str, str, str | None], ...]:
    """Resolve the order_by spec a request should sort by, BEFORE the
    ``order`` direction flip is applied.

    ``order_by=None`` (the common case) yields the contract-declared default
    (``cfg.order_by_spec``), preserving prior behavior exactly. A caller-
    supplied ``order_by`` (OMN-16290 -- previously accepted by FastAPI as an
    unrecognised query param and silently dropped, so every request served
    the fixed contract ordering regardless of what was requested) REPLACES
    the default entirely with the parsed, column-validated request spec.
    Raises :class:`InvalidOrderByError` on a malformed clause or a column not
    declared on this topic -- rejected outright, never silently ignored or
    coerced to the default (no-defensive-defaults).
    """
    if order_by is None:
        return cfg.order_by_spec
    try:
        return parse_order_by_clauses(order_by, cfg.columns)
    except ValueError as exc:
        raise InvalidOrderByError(str(exc)) from exc


def effective_order_by_spec(
    order_by_spec: tuple[tuple[str, str, str | None], ...], order: str | None
) -> tuple[tuple[str, str, str | None], ...]:
    """Apply a caller-requested direction flip to the FIRST sort column of
    ``order_by_spec`` (the contract default, or a caller-requested
    ``order_by`` override -- see :func:`base_order_by_spec`).

    Single source of truth for both the actual row order (fed to
    the row source's ``rows(order_spec=...)``) and the reported
    ``ordering`` string (:func:`reported_ordering`) -- computed once so the
    two can never diverge (CodeRabbit, OMN-15800: a caller-requested ``order``
    previously changed only the reported string, not the returned rows).
    Every sort key beyond the first is preserved verbatim (OMN-15799), the
    NULLS placement included (OMN-15800 defect A corrective round) -- a
    caller-requested direction flip changes ASC/DESC only, never the
    declared NULLS FIRST|LAST for that column.
    """
    if not order_by_spec:
        return ()
    first_column, first_direction, first_nulls = order_by_spec[0]
    if order is not None:
        normalised = order.strip().lower()
        if normalised == "asc":
            first_direction = "ASC"
        elif normalised == "desc":
            first_direction = "DESC"
    return ((first_column, first_direction, first_nulls), *order_by_spec[1:])


def reported_ordering(
    order_by_spec: tuple[tuple[str, str, str | None], ...],
    order_rank: ProjectionOrderRank | None = None,
) -> str:
    """Render the ``ordering`` response field FROM the typed, already-flipped spec.

    A declared ``order_rank`` (OMN-17215) is rendered first, as the SQL term it
    stands for, because it is the leading key the page is actually sorted by.
    """
    terms = [] if order_rank is None else [order_rank.sql_order_term()]
    terms.extend(
        f"{column} {direction}" + (f" NULLS {nulls}" if nulls else "")
        for column, direction, nulls in order_by_spec
    )
    if not terms:
        return "undefined"
    return ", ".join(terms)
