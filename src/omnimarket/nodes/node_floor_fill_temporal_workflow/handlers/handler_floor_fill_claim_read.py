# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read back, from ledger rows, which placed lanes wrote their CLAIM inside the claim window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_floor_fill_temporal_workflow.models.model_floor_fill_temporal import (
    ModelFloorFillClaimAnswer,
    ModelFloorFillClaimReadRequest,
    ModelFloorFillLaneClaim,
)


def _ts(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


class HandlerFloorFillClaimRead:
    """A lane is claimed when a ``CLAIM`` row with ``lane=<lane>`` is stamped inside [placed_at, placed_at + window]."""

    def handle(
        self, request: ModelFloorFillClaimReadRequest
    ) -> ModelFloorFillClaimAnswer:
        placed_at = _ts(request.placed_at)
        if placed_at is None:
            raise ValueError(f"placed_at is not a UTC stamp: {request.placed_at!r}")
        closes = placed_at + timedelta(seconds=request.window_seconds)
        found: dict[str, ModelFloorFillLaneClaim] = {}
        wanted = set(request.lanes)
        for row in request.rows:
            cells = [c.strip() for c in row.split("|")]
            if len(cells) < 3 or cells[1] != "CLAIM":
                continue
            stamp = _ts(cells[0])
            if stamp is None or not placed_at <= stamp <= closes:
                continue
            lanes = {
                c.removeprefix("lane=") for c in cells[2:] if c.startswith("lane=")
            }
            for lane in lanes & wanted:
                found.setdefault(
                    lane,
                    ModelFloorFillLaneClaim(
                        lane=lane, claimed_at=cells[0], row=row.strip()[:400]
                    ),
                )
        return ModelFloorFillClaimAnswer(
            claimed=tuple(found[lane] for lane in request.lanes if lane in found),
            unclaimed=tuple(lane for lane in request.lanes if lane not in found),
            read_at=request.read_at,
        )
