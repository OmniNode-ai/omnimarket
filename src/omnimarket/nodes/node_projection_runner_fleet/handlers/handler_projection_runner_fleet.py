# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive runner-fleet read-model rows from one observation. Pure; no I/O.

OMN-18768, the long pole of epic OMN-18767.

THE GAP THIS CLOSES
    A sweep of every ``onex.snapshot.projection.*`` topic across the runtime
    sources returns 60+ topics and not one runner, lane, fleet or host topic.
    The archived dashboard had none either. "What runners are running" is a
    question the platform could not answer from a projection at all --
    OMN-16943 records the structural cause: the runner monitor posts to a chat
    channel and emits no bus event, so nothing downstream can see the fleet.
    The emitter half of this ticket adds the event; this is the reducer over it.

Canonical definition-B shape
----------------------------
``handle(request: ModelRunnerFleetProjectionRequest) ->
ModelRunnerFleetProjectionResult``: a typed payload in, a typed payload out, no
event envelope in the core, no ``_db``, no clock, no database. The one fact the
derivation cannot know by itself -- which runners are already materialized --
arrives AS INPUT, resolved by the writer that owns the database. That is what
keeps the derivation a pure function: the same request always derives the same
rows.

WHY A DISAPPEARED RUNNER IS A TOMBSTONE AND NOT A STALE ROW
    The emitter publishes the WHOLE fleet every cycle, which is what makes an
    absence meaningful. A runner this host reported last cycle and does not
    report now has been deregistered. Leaving its last row behind would leave a
    deregistered runner reporting ``online`` forever -- worse than no row, since
    a capacity panel would count it. It is deleted, and the delete publishes a
    genuine tombstone so the bus-backed cache reclaims the key too.
"""

from __future__ import annotations

from omnimarket.nodes.node_projection_runner_fleet.models import (
    EnumRunnerStatus,
    ModelRunnerFleetClassRollup,
    ModelRunnerFleetProjectionRequest,
    ModelRunnerFleetProjectionResult,
    ModelRunnerFleetRow,
)

TABLE_FLEET = "runner_fleet_liveness"
FLEET_CONFLICT_KEY = "runner_name"


def derive_class_rollup(
    rows: tuple[ModelRunnerFleetRow, ...],
) -> tuple[ModelRunnerFleetClassRollup, ...]:
    """Count each label class separately. Pure.

    A BUSY runner counts toward ``online`` as well as ``busy`` -- it is up and
    it is working. Counting it only as busy would make a fully-saturated class
    read as a fully-offline one.
    """
    ordered: list[str] = []
    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = totals.get(row.label_class)
        if bucket is None:
            bucket = {"total": 0, "online": 0, "busy": 0, "offline": 0}
            totals[row.label_class] = bucket
            ordered.append(row.label_class)
        bucket["total"] += 1
        if row.status is EnumRunnerStatus.OFFLINE:
            bucket["offline"] += 1
        else:
            bucket["online"] += 1
            if row.status is EnumRunnerStatus.BUSY:
                bucket["busy"] += 1
    return tuple(
        ModelRunnerFleetClassRollup(label_class=name, **totals[name])
        for name in sorted(ordered)
    )


class HandlerProjectionRunnerFleet:
    """Pure def-B reducer over one runner-fleet observation."""

    def handle(
        self, request: ModelRunnerFleetProjectionRequest
    ) -> ModelRunnerFleetProjectionResult:
        observation = request.observation
        host = observation.host

        rows = tuple(
            ModelRunnerFleetRow(
                runner_name=runner.runner_name,
                runner_id=runner.runner_id,
                label_class=runner.label_class,
                labels=runner.labels,
                # TWO host facts, never one. `host` is where the runner
                # actually lives (its `host-<id>` label); `observing_host` is
                # the machine that took the observation. Collapsing them onto
                # the observer is what would have pointed an operator at .201
                # for a runner that was down on .105 -- measured live on
                # 2026-09-18, when that was the only offline runner in the
                # whole org pool.
                host=runner.host,
                observing_host=runner.observing_host or host,
                status=runner.status,
                # A job id on an idle runner is a contradiction, not a fact to
                # carry forward. Drop it rather than materialize a row that
                # says "not running anything, running job 4242".
                current_job_id=(
                    runner.current_job_id
                    if runner.status is EnumRunnerStatus.BUSY
                    else None
                ),
                observed_at=runner.observed_at or observation.observed_at,
            )
            # Deterministic order: the same observation derives the same result
            # byte for byte, not merely one that means the same thing.
            for runner in sorted(observation.runners, key=lambda r: r.runner_name)
        )

        observed_names = {row.runner_name for row in rows}
        tombstoned = tuple(
            sorted(
                name
                for name in request.known_runner_names
                if name not in observed_names
            )
        )

        return ModelRunnerFleetProjectionResult(
            rows=rows,
            tombstoned_runner_names=tombstoned,
            class_rollup=derive_class_rollup(rows),
        )


__all__ = [
    "FLEET_CONFLICT_KEY",
    "TABLE_FLEET",
    "HandlerProjectionRunnerFleet",
    "derive_class_rollup",
]
