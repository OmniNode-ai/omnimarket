# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam SL1: the producer's event and the reducer that reads it (OMN-19998).

The reducer's input is the producer's own class; the field set is pinned by name; the real producer's own output is decoded.
Each check returns what is wrong; each control is a named injected break that must make the same check report a problem.
"""

from __future__ import annotations

import logging
import typing
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from omnimarket.models.work_state_panel import ModelWorkStatePanelBody
from omnimarket.models.work_state_projection import (
    EnumWorkItemState,
    EnumWorkStateSource,
)
from omnimarket.models.work_state_projection.model_work_state_projected_event import (
    EnumWorkStateProjectorProfile,
    ModelWorkStateProjectedEvent,
    ModelWorkStateScheduleOverlay,
)
from omnimarket.nodes.node_work_state_panel_reducer.handlers import (
    service_work_state_panel_reducer as svc,
)
from omnimarket.nodes.node_work_state_panel_reducer.handlers.handler_work_state_panel_reducer import (
    HandlerWorkStatePanelReducer,
)
from omnimarket.nodes.node_work_state_projection_scheduled_effect.handlers.handler_work_state_projection_scheduled import (
    HandlerWorkStateProjectionScheduled,
)
from omnimarket.nodes.node_work_state_projection_scheduled_effect.protocols.protocol_work_state_projector import (
    UnreadWorkStateProjector,
)
from tests.nodes.node_work_state_panel_reducer.harness import (
    StateIoRowStore,
    run_event,
)
from tests.nodes.node_work_state_panel_reducer.support import (
    MARKER,
    NOW,
    entry,
    event,
    overlay,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import (
    T0 as TICK_T0,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import tick

pytestmark = pytest.mark.unit

MAPPED = {
    "computed_at",
    "as_of",
    "projector",
    "item_count",
    "counts",
    "ready_in_scope",
    "unread",
    "stale_sources",
}
DROPPED = {"tick_id"}


def problems_input_is_the_producer_model(model: type) -> list[str]:
    hint = typing.get_type_hints(HandlerWorkStatePanelReducer.handle)["request"]
    return (
        [] if hint is model else [f"handle() takes {hint!r}, not the producer's class"]
    )


def test_input_is_the_producer_model() -> None:
    assert problems_input_is_the_producer_model(ModelWorkStateProjectedEvent) == []


def test_control_a_copied_model_passes_equality_and_fails_identity() -> None:
    """Injected break `copied_model`: same field names, a different class."""
    copy = create_model(
        "Copy",
        __base__=BaseModel,
        **dict.fromkeys(ModelWorkStateProjectedEvent.model_fields, (Any, None)),
    )
    assert set(copy.model_fields) == set(ModelWorkStateProjectedEvent.model_fields)
    assert problems_input_is_the_producer_model(copy) != []


def problems_event_fields(event_model: type[BaseModel]) -> list[str]:
    fields = set(event_model.model_fields)
    problems: list[str] = []
    if fields != MAPPED | DROPPED:
        problems.append(
            f"unmapped or missing producer fields: {sorted(fields ^ (MAPPED | DROPPED))}"
        )
    if set(ModelWorkStatePanelBody.model_fields) != MAPPED:
        problems.append("the body's field set is not the mapped set")
    return problems


def test_producer_event_fields() -> None:
    assert problems_event_fields(ModelWorkStateProjectedEvent) == []


def test_control_a_producer_field_added() -> None:
    """Injected break `publication_field_added` (omnimarket#5704 adds that field)."""
    wider = create_model(
        "Wider", __base__=ModelWorkStateProjectedEvent, publication=(str, "")
    )
    assert problems_event_fields(wider) != []


def problems_old_consumer_new_producer(
    model: type[BaseModel], caplog: pytest.LogCaptureFixture
) -> list[str]:
    store = StateIoRowStore()
    handler = HandlerWorkStatePanelReducer(clock=lambda: NOW, overlay=overlay())
    run_event(store, handler, event(100))
    before = dict(store.rows)
    wire = event(200).model_dump(mode="json") | {"publication": MARKER}
    try:
        decoded = model.model_validate(wire)
    except ValidationError:
        pass
    else:
        run_event(store, handler, decoded)  # type: ignore[arg-type]
        return ["an event carrying an unmapped field was accepted"]
    if store.rows != before or MARKER in caplog.text:
        return ["the row changed or the marker was logged"]
    return []


def test_old_consumer_new_producer_wire(caplog: pytest.LogCaptureFixture) -> None:
    """Coordinated rollout: the older reading rejects the newer producer's field and leaves the row alone."""
    assert (
        problems_old_consumer_new_producer(ModelWorkStateProjectedEvent, caplog) == []
    )


def test_control_a_reading_that_ignores_unknown_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Injected break `extra_ignore`."""

    class Lenient(ModelWorkStateProjectedEvent):
        model_config = ConfigDict(frozen=True, extra="ignore")

    assert problems_old_consumer_new_producer(Lenient, caplog) != []


def real_unread_event() -> ModelWorkStateProjectedEvent:
    handler = HandlerWorkStateProjectionScheduled(
        schedule=ModelWorkStateScheduleOverlay(
            run_interval_seconds=300,
            projector=EnumWorkStateProjectorProfile.FILE_SOURCES,
        ),
        projector=UnreadWorkStateProjector(),
    )
    produced = handler.handle(tick(TICK_T0))
    assert produced is not None
    return produced


def problems_real_unread(body_from_event: Any) -> list[str]:
    produced = real_unread_event()
    decision = svc.WorkStatePanelReducerService().decide_publication(
        None, produced, produced.as_of, entry()
    )
    problems: list[str] = []
    if (
        decision.outcome.value != "accepted"
        or decision.row is None
        or decision.row.body is None
    ):
        return [f"the real unread event was {decision.outcome.value}"]
    body = decision.row.body
    if set(body.unread) != set(EnumWorkStateSource):
        problems.append("the body does not hold all seven unread sources")
    if set(body.counts) != set(EnumWorkItemState) or any(body.counts.values()):
        problems.append("the body does not hold every item state at zero")
    return problems


def test_real_unread_event_accepted() -> None:
    assert problems_real_unread(svc.body_from_event) == []


def test_control_a_body_that_drops_zero_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Injected break `drops_zero_counts`."""
    real = svc.body_from_event

    def dropping(event_in: ModelWorkStateProjectedEvent) -> ModelWorkStatePanelBody:
        trimmed = event_in.model_copy(
            update={"counts": {k: v for k, v in event_in.counts.items() if v}}
        )
        return real(trimmed)

    monkeypatch.setattr(svc, "body_from_event", dropping)
    assert problems_real_unread(dropping) != []


def test_dispatch_of_an_undecodable_envelope_leaves_the_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An event failing the producer model's own decode never reaches the handler (the runtime validates first)."""
    wire = event(100).model_dump(mode="json") | {"surprise": MARKER}
    with caplog.at_level(logging.DEBUG), pytest.raises(ValidationError):
        ModelWorkStateProjectedEvent.model_validate(wire)
    assert MARKER not in caplog.text
