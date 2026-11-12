# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer group state REDUCER handler (OMN-23992).

Pure fold: ``(prior record, census event) -> new record``. The reducer keeps the first census time of the
current unbroken run of EMPTY readings per group, so no file is needed. No I/O, no bus, no clock: times come
from the event. Every bound (empty-since bound, reap cap, patterns) comes from the node overlay.
"""

from __future__ import annotations

import re
from datetime import datetime
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.events.consumer_group_state import (
    ModelGroupCensusEvent,
    ModelGroupState,
    ModelGroupStateRecord,
    ModelGroupStateReduceCommand,
    decision_digest,
)
from omnimarket.nodes.node_consumer_group_state_reducer.overlay import (
    ModelGroupStateOverlay,
    load_overlay,
)

HANDLER_ID = "consumer-group-state-reducer"


class GroupStateTransitionError(ValueError):
    """A census the record does not accept: empty, another lane, or out of order."""


def _refuse(
    prior: ModelGroupStateRecord | None, event: ModelGroupCensusEvent, why: str
) -> GroupStateTransitionError:
    lane = event.bus_lane
    return GroupStateTransitionError(f"bus lane {lane}: {why}")


def _prior_empty_since(
    prior: ModelGroupStateRecord | None, name: str
) -> datetime | None:
    if prior is None:
        return None
    for group in prior.groups:
        if group.name == name:
            return group.empty_since
    return None


def fold(
    prior: ModelGroupStateRecord | None,
    event: ModelGroupCensusEvent,
    overlay: ModelGroupStateOverlay,
) -> ModelGroupStateRecord:
    """Fold one census into the bus lane's group record. Pure."""
    if not event.groups:
        raise _refuse(prior, event, "an empty census is not a reading")
    if prior is not None and prior.bus_lane != event.bus_lane:
        raise _refuse(prior, event, "the census is for another bus lane")
    if prior is not None and event.observed_at < prior.updated_at:
        raise _refuse(prior, event, "the census is out of order")

    groups: list[ModelGroupState] = []
    for name, state in event.groups.items():
        if state == "EMPTY":
            empty_since = _prior_empty_since(prior, name)
            if empty_since is None:
                empty_since = event.observed_at
        else:
            empty_since = None
        groups.append(
            ModelGroupState(
                name=name,
                state=state,
                empty_since=empty_since,
                last_seen=event.observed_at,
            )
        )
    groups.sort(key=lambda g: g.name)

    empty_since_by_name = {
        g.name: g.empty_since for g in groups if g.empty_since is not None
    }
    reapable = [re.compile(p) for p in overlay.reapable_patterns]
    protected = [re.compile(p) for p in overlay.protected_patterns]

    due: list[str] = []
    for group in groups:
        if group.state != "EMPTY" or group.empty_since is None:
            continue
        if (
            event.observed_at - group.empty_since
        ).total_seconds() < overlay.empty_since_bound_seconds:
            continue
        if any(p.search(group.name) for p in protected):
            continue
        if not any(p.search(group.name) for p in reapable):
            continue
        due.append(group.name)
    due.sort()
    due = due[: overlay.max_reap_per_decision]

    digest = decision_digest(
        event.bus_lane,
        event.correlation_id,
        tuple(due),
        empty_since_by_name,
        event.observed_at,
    )

    return ModelGroupStateRecord(
        bus_lane=event.bus_lane,
        updated_at=event.observed_at,
        census_correlation_id=event.correlation_id,
        groups=tuple(groups),
        reap_due=tuple(due),
        decided_by="reducer",
        decision_digest=digest,
    )


class HandlerGroupState:
    """Pure reducer: fold one census into the bus lane's group record projection."""

    def __init__(self, overlay: ModelGroupStateOverlay | None = None) -> None:
        self._overlay = overlay

    async def handle(
        self, request: ModelGroupStateReduceCommand
    ) -> ModelHandlerOutput[None]:
        """Fold the census and emit the new record as the projection."""
        if self._overlay is None:
            self._overlay = load_overlay()
        record = fold(request.prior, request.event, self._overlay)
        return ModelHandlerOutput.for_reducer(
            input_envelope_id=uuid4(),
            correlation_id=request.event.correlation_id,
            handler_id=HANDLER_ID,
            projections=(record,),
        )


__all__: list[str] = [
    "HANDLER_ID",
    "GroupStateTransitionError",
    "HandlerGroupState",
    "fold",
]
