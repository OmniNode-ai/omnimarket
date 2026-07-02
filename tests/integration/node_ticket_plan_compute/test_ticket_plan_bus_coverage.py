# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-state COMPUTE coverage for node_ticket_plan_compute, driven over
the canonical in-memory bus.

OMN-13674 (cluster wave-sweep-audit-compute). The COMPUTE handler
``HandlerTicketPlanCompute`` is dispatched through ``LocalRuntimeBusAdapter`` over
``EventBusInmemory`` (via the ``integration_event_bus`` fixture): a
``ModelTicketPlanRequest`` lands on the contract-declared command topic
``onex.cmd.omnimarket.ticket-plan-start.v1`` and the runtime auto-emits the
``ModelTicketPlanResult`` onto the contract-declared terminal topic
``onex.evt.omnimarket.ticket-plan-completed.v1``.

COMPUTE DoD:
  * every declared output field populated on the terminal payload -- the
    ``tickets`` list (title / description / phase / depends_on / labels) and the
    ``parse_warnings`` list;
  * every parse branch exercised: the three title/description separators
    (``em-dash`` / ``--`` / colon), checkbox stripping, ``depends on`` and
    ``labels`` extraction, phase assignment from a heading, and the
    untitled-fallback path;
  * a negative control: a plan with no ticket bullets MUST emit the
    ``"no ticket bullets found"`` warning and yield zero tickets.

The handler is pure (no Linear API calls); the caller supplies raw plan text.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_ticket_plan_compute.handlers.handler_ticket_plan_compute import (
    HandlerTicketPlanCompute,
)
from omnimarket.nodes.node_ticket_plan_compute.models.model_ticket_plan_request import (
    ModelTicketPlanRequest,
)
from omnimarket.nodes.node_ticket_plan_compute.models.model_ticket_plan_result import (
    ModelTicketPlanResult,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

# Contract-declared topics (node_ticket_plan_compute/contract.yaml).
_START_TOPIC = "onex.cmd.omnimarket.ticket-plan-start.v1"
_COMPLETED_TOPIC = "onex.evt.omnimarket.ticket-plan-completed.v1"


async def _run_over_bus(
    bus: Any, request: ModelTicketPlanRequest
) -> ModelTicketPlanResult:
    """Publish a ticket-plan request onto the declared command topic and return
    the terminal ``ModelTicketPlanResult`` parsed off the declared terminal topic."""
    adapter = LocalRuntimeBusAdapter(
        handler=HandlerTicketPlanCompute(),
        handler_name="ticket-plan-compute",
        input_model_cls=ModelTicketPlanRequest,
        output_topic=_COMPLETED_TOPIC,
        bus=bus,
    )
    await bus.subscribe(
        _START_TOPIC,
        on_message=adapter.on_message,
        group_id="omnimarket-ticket-plan-test",
    )
    await bus.publish(
        _START_TOPIC,
        key=None,
        value=request.model_dump_json().encode("utf-8"),
    )
    history = await bus.get_event_history(topic=_COMPLETED_TOPIC)
    assert len(history) == 1, f"expected 1 terminal event on {_COMPLETED_TOPIC}"
    return ModelTicketPlanResult.model_validate(json.loads(history[-1].value))


@pytest.mark.integration
async def test_ticket_plan_full_parse_branches_over_bus(
    integration_event_bus: Any,
) -> None:
    """Every parse branch is exercised in one plan: heading phase, the three
    separators, checkbox strip, depends_on and labels extraction, untitled
    fallback."""
    plan = "\n".join(
        [
            "# Phase One",
            "- [ ] Build the projection reducer -- fold every declared event",
            "- Wire the dispatcher: route each declared handler; labels: "
            "backend, infra; depends on: Build the projection reducer",
            "- [x] Ship em-dash ticket — a done body with an em-dash split",
            "## Phase Two",
            "- [ ]",
        ]
    )
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(bus, ModelTicketPlanRequest(plan_text=plan))

        by_title = {t.title: t for t in result.tickets}

        # em-dash / -- / colon separators all produced tickets.
        reducer = by_title["Build the projection reducer"]
        assert reducer.phase == "Phase One"
        assert reducer.description == "fold every declared event"
        assert reducer.depends_on == []
        assert reducer.labels == []

        dispatcher = by_title["Wire the dispatcher"]
        assert dispatcher.phase == "Phase One"
        assert dispatcher.depends_on == ["Build the projection reducer"]
        assert dispatcher.labels == ["backend", "infra"]
        # depends_on and labels clauses are stripped out of the description.
        assert "depends on" not in dispatcher.description.lower()
        assert "labels" not in dispatcher.description.lower()

        emdash = by_title["Ship em-dash ticket"]
        assert emdash.description == "a done body with an em-dash split"

        # The checkbox-only bullet "[ ]" has no title -> untitled fallback, and
        # it belongs to the second heading phase.
        untitled = [t for t in result.tickets if t.title.startswith("Untitled ticket")]
        assert len(untitled) == 1
        assert untitled[0].phase == "Phase Two"

        assert len(result.tickets) == 4
    finally:
        await bus.close()


@pytest.mark.integration
async def test_ticket_plan_double_dash_separator_over_bus(
    integration_event_bus: Any,
) -> None:
    """The ``--`` separator splits title from description."""
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(
            bus,
            ModelTicketPlanRequest(plan_text="- Title here -- body text past the dash"),
        )
        assert len(result.tickets) == 1
        assert result.tickets[0].title == "Title here"
        assert result.tickets[0].description == "body text past the dash"
    finally:
        await bus.close()


@pytest.mark.integration
async def test_ticket_plan_no_bullets_warns_over_bus(
    integration_event_bus: Any,
) -> None:
    """Negative control: a plan with no ticket bullets emits the documented
    warning and yields zero tickets."""
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(
            bus,
            ModelTicketPlanRequest(plan_text="# Just a heading\n\nsome prose only"),
        )
        assert result.tickets == []
        assert "no ticket bullets found" in result.parse_warnings
    finally:
        await bus.close()


@pytest.mark.integration
async def test_ticket_plan_numbered_bullets_and_no_phase_over_bus(
    integration_event_bus: Any,
) -> None:
    """Numbered bullets parse and, with no preceding heading, phase is null."""
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(
            bus,
            ModelTicketPlanRequest(plan_text="1. First task\n2) Second task"),
        )
        assert [t.title for t in result.tickets] == ["First task", "Second task"]
        assert all(t.phase is None for t in result.tickets)
    finally:
        await bus.close()


@pytest.mark.integration
async def test_ticket_plan_epic_and_team_ids_do_not_break_parse_over_bus(
    integration_event_bus: Any,
) -> None:
    """Optional epic_id / team_id are accepted; parsing is unaffected."""
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(
            bus,
            ModelTicketPlanRequest(
                plan_text="- Only ticket: a single body",
                epic_id="OMN-9999",
                team_id="TEAM-1",
            ),
        )
        assert len(result.tickets) == 1
        assert result.tickets[0].title == "Only ticket"
        assert result.tickets[0].description == "a single body"
    finally:
        await bus.close()


@pytest.mark.integration
async def test_ticket_plan_pure_handler_matches_bus_result(
    integration_event_bus: Any,
) -> None:
    """The in-process pure return equals the bus-transited terminal payload."""
    request = ModelTicketPlanRequest(
        plan_text="# Phase\n- A ticket: with a body; labels: x"
    )
    direct = HandlerTicketPlanCompute().handle(request)
    assert direct.tickets[0].labels == ["x"]

    bus = integration_event_bus
    await bus.start()
    try:
        transited = await _run_over_bus(bus, request)
        assert transited.model_dump() == direct.model_dump()
    finally:
        await bus.close()
