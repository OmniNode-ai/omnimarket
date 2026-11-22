# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure decision for the done-items coverage alarm: no I/O and no clock read (OMN-26435)."""

from __future__ import annotations

from omnimarket.nodes.node_projection_freshness_alert_effect.models import (
    EnumFreshnessState,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.models.model_done_items_coverage import (
    ModelCoverageVerdict,
)


def classify_done_items_coverage(
    *,
    check_id: str,
    demand_done_tickets: int | None,
    unprojected_tickets: int | None,
    refused_rows: int | None = 0,
    error: str | None = None,
) -> ModelCoverageVerdict:
    """Classify the done-items coverage state from the supplied counts and error."""
    if error is not None:
        return ModelCoverageVerdict(
            check_id=check_id,
            state=EnumFreshnessState.UNKNOWN,
            demand_done_tickets=None,
            unprojected_tickets=None,
            error=error,
        )

    if demand_done_tickets is None:
        return ModelCoverageVerdict(
            check_id=check_id,
            state=EnumFreshnessState.UNKNOWN,
            demand_done_tickets=None,
            unprojected_tickets=None,
            error="demand_done_tickets is None",
        )

    if unprojected_tickets is None:
        return ModelCoverageVerdict(
            check_id=check_id,
            state=EnumFreshnessState.UNKNOWN,
            demand_done_tickets=None,
            unprojected_tickets=None,
            error="unprojected_tickets is None",
        )

    if demand_done_tickets < 0:
        return ModelCoverageVerdict(
            check_id=check_id,
            state=EnumFreshnessState.UNKNOWN,
            demand_done_tickets=None,
            unprojected_tickets=None,
            error="demand_done_tickets is negative",
        )

    if unprojected_tickets < 0:
        return ModelCoverageVerdict(
            check_id=check_id,
            state=EnumFreshnessState.UNKNOWN,
            demand_done_tickets=None,
            unprojected_tickets=None,
            error="unprojected_tickets is negative",
        )

    if refused_rows is None or refused_rows < 0:
        return ModelCoverageVerdict(
            check_id=check_id,
            state=EnumFreshnessState.UNKNOWN,
            demand_done_tickets=None,
            unprojected_tickets=None,
            error="refused_rows is missing or negative",
        )

    if refused_rows > 0:
        return ModelCoverageVerdict(
            check_id=check_id,
            state=EnumFreshnessState.UNKNOWN,
            demand_done_tickets=demand_done_tickets,
            unprojected_tickets=unprojected_tickets,
            refused_rows=refused_rows,
            error=f"{refused_rows} ledger rows could not be parsed, so coverage is unknown",
        )

    if demand_done_tickets == 0:
        return ModelCoverageVerdict(
            check_id=check_id,
            state=EnumFreshnessState.IDLE,
            demand_done_tickets=demand_done_tickets,
            unprojected_tickets=unprojected_tickets,
            refused_rows=refused_rows,
        )

    if unprojected_tickets > demand_done_tickets:
        return ModelCoverageVerdict(
            check_id=check_id,
            state=EnumFreshnessState.UNKNOWN,
            demand_done_tickets=demand_done_tickets,
            unprojected_tickets=unprojected_tickets,
            refused_rows=refused_rows,
            error="unprojected_tickets exceeds demand_done_tickets",
        )

    if unprojected_tickets > 0:
        return ModelCoverageVerdict(
            check_id=check_id,
            state=EnumFreshnessState.STALE,
            demand_done_tickets=demand_done_tickets,
            unprojected_tickets=unprojected_tickets,
            refused_rows=refused_rows,
        )

    return ModelCoverageVerdict(
        check_id=check_id,
        state=EnumFreshnessState.FRESH,
        demand_done_tickets=demand_done_tickets,
        unprojected_tickets=unprojected_tickets,
        refused_rows=refused_rows,
    )


__all__ = ["classify_done_items_coverage"]
