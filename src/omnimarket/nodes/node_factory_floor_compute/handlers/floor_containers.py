# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The selected line shows each lane's application containers from the lane container budget read."""

from __future__ import annotations

from omnimarket.models.model_factory_floor_input import ModelFactoryFloorRequest
from omnimarket.models.model_lane_container_budget import (
    ModelLaneContainerBudgetVerdict,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFloorApplicationContainers,
    ModelFloorLaneContainers,
    ModelFloorLine,
)


def _rows(
    verdict: ModelLaneContainerBudgetVerdict,
) -> tuple[ModelFloorLaneContainers, ...]:
    alarms = {(alarm.host, alarm.lane): alarm.key for alarm in verdict.alarms}
    rows = tuple(
        ModelFloorLaneContainers(
            host=lane.host,
            lane=lane.lane,
            status=lane.status,
            observed=lane.observed,
            budget=lane.budget,
            alarm_key=alarms.get((lane.host, lane.lane), ""),
            detail=lane.detail,
        )
        for lane in verdict.lanes
    )
    # A host with no declared budget has no lane rows; its alarm still shows, under the lane the alarm names.
    covered = {(row.host, row.lane) for row in rows}
    return rows + tuple(
        ModelFloorLaneContainers(
            host=alarm.host,
            lane=alarm.lane,
            status=alarm.status,
            observed=alarm.observed,
            budget=alarm.budget,
            alarm_key=alarm.key,
            detail=alarm.detail,
        )
        for alarm in verdict.alarms
        if (alarm.host, alarm.lane) not in covered
    )


def with_application_containers(
    request: ModelFactoryFloorRequest, lines: tuple[ModelFloorLine, ...]
) -> tuple[ModelFloorLine, ...]:
    """Attach the container read to the line the selection names; every other line is returned unchanged."""
    shown = (
        ModelFloorApplicationContainers(state="unread", lanes=())
        if request.lane_containers is None
        else ModelFloorApplicationContainers(
            state="read",
            lanes=tuple(
                sorted(
                    (
                        row
                        for verdict in request.lane_containers
                        for row in _rows(verdict)
                    ),
                    key=lambda row: (row.host, row.lane),
                )
            ),
        )
    )
    token = request.selection.container_read_line
    return tuple(
        line.model_copy(update={"application_containers": shown})
        if token and token in line.line_id
        else line
        for line in lines
    )
