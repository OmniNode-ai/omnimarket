# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane's live application containers against the budget declared for it (one-runtime plan, task T2).

Nothing compared a lane's running application containers with a declared count, so a second container appeared unseen
until someone found it in a container dashboard. The handler reads one host's recorded container listing, attributes
each running container to a lane by its compose project, drops infrastructure the declaration names, and judges the
count against that lane's declared budget.

Over budget is FAIL. A host that was not read is UNKNOWN, and UNKNOWN fails too; there is no warning status. Each
failing lane raises one alarm keyed ``lane-budget:<host>:<lane>``, and ``upsert_alarms`` rewrites the row under that
key, so a rerun never adds a second row.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from omnimarket.models.model_lane_container_budget import (
    ModelLaneContainerBudgetAlarm,
    ModelLaneContainerBudgetDeclaration,
    ModelLaneContainerBudgetLane,
    ModelLaneContainerBudgetRequest,
    ModelLaneContainerBudgetVerdict,
)

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
RUNNING_STATE = "running"
UNDECLARED_LANE = "undeclared"


def alarm_key(host: str, lane: str) -> str:
    return f"lane-budget:{host}:{lane}"


def parse_labels(raw: object) -> dict[str, str]:
    """Labels as the listing carries them: a mapping, or the CLI's comma-joined ``k=v`` string.

    A label value may itself contain commas (a list of compose files), so a segment with no ``=`` continues the
    value before it.
    """
    if isinstance(raw, Mapping):
        return {str(k): str(v) for k, v in raw.items()}
    labels: dict[str, str] = {}
    current: str | None = None
    for segment in str(raw or "").split(","):
        if "=" in segment:
            key, value = segment.split("=", 1)
            current = key.strip()
            labels[current] = value.strip()
        elif current is not None and segment:
            labels[current] = f"{labels[current]},{segment}"
    return labels


@dataclass(frozen=True)
class _Container:
    name: str
    state: str
    project: str


def parse_listing(listing: str) -> tuple[_Container, ...] | str:
    """The listing's containers, or the reason it cannot be read."""
    containers: list[_Container] = []
    for number, line in enumerate(listing.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return f"listing line {number} is not JSON"
        if not isinstance(row, dict):
            return f"listing line {number} is not a JSON object"
        name = str(row.get("Names") or "").lstrip("/").strip()
        if not name:
            return f"listing line {number} names no container"
        labels = parse_labels(row.get("Labels"))
        containers.append(
            _Container(
                name=name,
                state=str(row.get("State") or ""),
                project=labels.get(COMPOSE_PROJECT_LABEL, ""),
            )
        )
    if not containers:
        return "the container listing is empty"
    return tuple(containers)


def _unknown(
    declaration: ModelLaneContainerBudgetDeclaration, reason: str
) -> ModelLaneContainerBudgetLane:
    return ModelLaneContainerBudgetLane(
        host=declaration.host,
        lane=declaration.lane,
        status="UNKNOWN",
        observed=None,
        budget=declaration.application_container_budget,
        containers=(),
        detail=f"UNKNOWN, which fails: {reason}",
    )


def _judge(
    declaration: ModelLaneContainerBudgetDeclaration,
    containers: tuple[_Container, ...],
) -> ModelLaneContainerBudgetLane:
    names = tuple(
        sorted(
            c.name
            for c in containers
            if c.project == declaration.compose_project
            and c.state == RUNNING_STATE
            and not any(m in c.name for m in declaration.infrastructure_markers)
        )
    )
    budget = declaration.application_container_budget
    return ModelLaneContainerBudgetLane(
        host=declaration.host,
        lane=declaration.lane,
        status="FAIL" if len(names) > budget else "PASS",
        observed=len(names),
        budget=budget,
        containers=names,
        detail=f"{len(names)} application containers running, budget {budget}",
    )


def _alarm(lane: ModelLaneContainerBudgetLane) -> ModelLaneContainerBudgetAlarm:
    return ModelLaneContainerBudgetAlarm(
        key=alarm_key(lane.host, lane.lane),
        host=lane.host,
        lane=lane.lane,
        status="FAIL" if lane.status == "FAIL" else "UNKNOWN",
        observed=lane.observed,
        budget=lane.budget,
        containers=lane.containers,
        detail=lane.detail,
    )


class HandlerLaneContainerBudget:
    def handle(
        self, request: ModelLaneContainerBudgetRequest
    ) -> ModelLaneContainerBudgetVerdict:
        declarations = tuple(d for d in request.declarations if d.host == request.host)
        if not declarations:
            alarm = ModelLaneContainerBudgetAlarm(
                key=alarm_key(request.host, UNDECLARED_LANE),
                host=request.host,
                lane=UNDECLARED_LANE,
                status="UNKNOWN",
                observed=None,
                budget=None,
                containers=(),
                detail=f"UNKNOWN, which fails: no application container budget is declared for host {request.host}",
            )
            return ModelLaneContainerBudgetVerdict(
                host=request.host,
                status="UNKNOWN",
                failed=True,
                lanes=(),
                alarms=(alarm,),
            )
        listing = (
            f"the host read failed: {request.read_error}"
            if request.read_error
            else parse_listing(request.docker_ps)
        )
        lanes = tuple(
            _unknown(d, listing) if isinstance(listing, str) else _judge(d, listing)
            for d in sorted(declarations, key=lambda d: d.lane)
        )
        statuses = {lane.status for lane in lanes}
        status = (
            "FAIL"
            if "FAIL" in statuses
            else "UNKNOWN"
            if "UNKNOWN" in statuses
            else "PASS"
        )
        return ModelLaneContainerBudgetVerdict(
            host=request.host,
            status=status,
            failed=status != "PASS",
            lanes=lanes,
            alarms=tuple(_alarm(lane) for lane in lanes if lane.status != "PASS"),
        )

    def upsert_alarms(
        self,
        rows: tuple[ModelLaneContainerBudgetAlarm, ...],
        verdict: ModelLaneContainerBudgetVerdict,
    ) -> tuple[ModelLaneContainerBudgetAlarm, ...]:
        """The alarm rows after this verdict: a row with the same key is replaced, never added beside."""
        keyed = {row.key: row for row in rows}
        keyed.update({alarm.key: alarm for alarm in verdict.alarms})
        return tuple(keyed[key] for key in sorted(keyed))
