# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the scheduled work-state projection (OMN-22935): the schedule gate, the event, the refusals."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.work_state_projection import (
    EnumWorkStateProjectorProfile,
    EnumWorkStateSource,
    ModelTicketSource,
    ModelWorkScope,
    ModelWorkStateRequest,
    ModelWorkStateResult,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from omnimarket.nodes.node_work_state_projection_scheduled_effect.handlers.handler_work_state_projection_scheduled import (
    HandlerWorkStateProjectionScheduled,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import (
    T0,
    rules,
    schedule,
    tick,
)

pytestmark = pytest.mark.unit


class RealComputeProjector:
    """A projector over the real compute handler with two tickets: one available, one blocked on the first."""

    def __init__(self) -> None:
        self.calls = 0

    def project(self, now: datetime) -> ModelWorkStateResult:
        self.calls += 1
        return HandlerWorkStateProjection().handle(
            ModelWorkStateRequest(
                now=now,
                rules=rules(),
                scope=ModelWorkScope(in_scope_projects=("Zephyr",)),
                tickets=(
                    ModelTicketSource(
                        ticket_id="ZEP-1", state_type="backlog", project="Zephyr"
                    ),
                    ModelTicketSource(
                        ticket_id="ZEP-2",
                        state_type="backlog",
                        project="Zephyr",
                        requires=("ZEP-1",),
                    ),
                ),
            )
        )


class FailingProjector:
    def project(self, now: datetime) -> ModelWorkStateResult:
        raise RuntimeError("source tree refused")


def test_golden_a_due_tick_emits_the_projected_event_for_that_tick() -> None:
    first = tick(T0)
    event = HandlerWorkStateProjectionScheduled(
        schedule=schedule(), projector=RealComputeProjector()
    ).handle(first)
    assert event is not None
    assert event.computed_at == T0
    assert event.tick_id == str(first.tick_id)
    assert event.item_count == 2
    assert event.counts["available"] == 1
    assert event.counts["blocked"] == 1
    assert sum(event.counts.values()) == 2
    assert event.ready_in_scope == 1
    assert event.unread == ()


def test_golden_the_schedule_gate_runs_once_per_interval() -> None:
    projector = RealComputeProjector()
    h = HandlerWorkStateProjectionScheduled(schedule=schedule(), projector=projector)
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=59))) is None
    assert projector.calls == 1
    assert h.handle(tick(T0 + timedelta(seconds=60))) is not None
    assert projector.calls == 2


def test_golden_the_in_memory_profile_names_every_source_unread_and_offers_nothing() -> (
    None
):
    event = HandlerWorkStateProjectionScheduled(schedule=schedule()).handle(tick(T0))
    assert event is not None
    assert event.projector is EnumWorkStateProjectorProfile.UNREAD
    assert {u.split(":", 1)[0] for u in event.unread} == {
        s.value for s in EnumWorkStateSource
    }
    assert event.item_count == 0
    assert event.ready_in_scope == 0


def test_error_a_skipped_tick_does_not_move_the_last_run() -> None:
    h = HandlerWorkStateProjectionScheduled(
        schedule=schedule(), projector=RealComputeProjector()
    )
    h.handle(tick(T0))
    assert h.handle(tick(T0 + timedelta(seconds=30))) is None
    assert h.handle(tick(T0 + timedelta(seconds=60))) is not None


def test_error_a_projector_that_raises_leaves_the_last_run_so_the_next_tick_retries() -> (
    None
):
    h = HandlerWorkStateProjectionScheduled(
        schedule=schedule(), projector=FailingProjector()
    )
    with pytest.raises(RuntimeError, match="source tree refused"):
        h.handle(tick(T0))
    with pytest.raises(RuntimeError, match="source tree refused"):
        h.handle(tick(T0 + timedelta(seconds=1)))


def test_error_no_schedule_overlay_refuses_naming_the_setting() -> None:
    h = HandlerWorkStateProjectionScheduled(environ={})
    with pytest.raises(
        NodeOverlayNotFoundError, match="ONEX_WORK_STATE_SCHEDULE_OVERLAY"
    ):
        h.handle(tick(T0))


def test_error_a_zero_interval_is_refused() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        schedule(run_interval_seconds=0)


def test_error_an_unknown_projector_profile_is_refused() -> None:
    with pytest.raises(ValidationError, match="projector"):
        schedule(projector="database")


def test_error_the_file_sources_profile_without_the_rules_overlay_refuses_naming_it(
    tmp_path,
) -> None:
    """The lab's state on 2026-11-10: the rules overlay was not in the overlays repository, so the projection refused."""
    h = HandlerWorkStateProjectionScheduled(
        schedule=schedule(projector="file_sources"),
        environ={"OMNI_HOME": str(tmp_path)},
    )
    with pytest.raises(NodeOverlayNotFoundError, match="ONEX_WORK_STATE_OVERLAY"):
        h.handle(tick(T0))
