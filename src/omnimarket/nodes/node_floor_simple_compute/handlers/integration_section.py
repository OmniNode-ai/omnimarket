# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's integration section from the integration projection as the cycle read it (OMN-23899). Pure.

The projection already holds the counts and the groups (node_integration_projection_compute decided them); this draws
them with the overlay's words: each group titled, the not-running rows oldest merge first, at most ``max_rows`` of them.
"""

from __future__ import annotations

from omnimarket.models.floor_17a.model_floor_integration import (
    ModelFloorIntegrationGroupLine,
    ModelFloorIntegrationIn,
    ModelFloorIntegrationOverlay,
    ModelFloorIntegrationRow,
    ModelFloorIntegrationSection,
)

NOT_CONFIGURED = "the floor overlay declares no integration section"
NOT_READ = "this cycle did not read the integration projection"


def build_integration_section(
    overlay: ModelFloorIntegrationOverlay | None,
    reading: ModelFloorIntegrationIn | None,
) -> ModelFloorIntegrationSection:
    """The section: not configured, unread with the reason, or read with counts, groups and rows."""
    if overlay is None:
        return ModelFloorIntegrationSection(
            status="not_configured", reason=NOT_CONFIGURED
        )
    if reading is None:
        return ModelFloorIntegrationSection(
            status="unread", title=overlay.title, reason=NOT_READ
        )
    if reading.status == "unread" or reading.projection is None:
        return ModelFloorIntegrationSection(
            status="unread",
            title=overlay.title,
            reason=reading.reason,
            as_of=reading.as_of,
            age_s=reading.age_s,
        )
    projection = reading.projection
    order = [key for group in projection.groups for key in group.keys]
    oldest_first = sorted(order, key=lambda key: (projection.rows[key].merged_at, key))
    rows = tuple(
        ModelFloorIntegrationRow(
            key=key,
            title=projection.rows[key].title,
            merged_at=projection.rows[key].merged_at,
            target=projection.rows[key].target,
            state=projection.rows[key].state,
            gap_class=projection.rows[key].gap_class,
            gap=projection.rows[key].gap,
            step=projection.rows[key].step,
            owner=projection.rows[key].owner,
            companions=projection.rows[key].companions,
        )
        for key in oldest_first
    )
    groups = tuple(
        ModelFloorIntegrationGroupLine(
            gap_class=group.gap_class,
            title=overlay.group_titles[group.gap_class],
            count=len(group.keys),
            keys=group.keys,
        )
        for group in projection.groups
    )
    return ModelFloorIntegrationSection(
        status="read",
        title=overlay.title,
        as_of=reading.as_of,
        age_s=reading.age_s,
        counts=projection.counts,
        groups=groups,
        rows=rows[: overlay.max_rows],
        rows_total=len(rows),
    )


__all__ = ["NOT_CONFIGURED", "NOT_READ", "build_integration_section"]
