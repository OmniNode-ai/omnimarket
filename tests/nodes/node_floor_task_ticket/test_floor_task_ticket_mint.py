# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor-task ticket mint, both nodes composed on a stubbed Linear (plan T11, criteria 1 to 3).

``-k mint_payload_passes_gate`` runs ``node_create_ticket``'s own gate for real: the omnibase_core ticket-creation
validator, in process, so it runs on every runner. The rest run on a stub gate.
"""

from __future__ import annotations

import pytest
from omnibase_spi.contracts.services import ModelFloorTicketCreateRequest

from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_inmemory_floor_ticket_store import (
    InMemoryFloorTicketStore,
)
from omnimarket.nodes.node_floor_task_ticket_effect.handlers import (
    HandlerFloorTaskTicketMint,
)
from omnimarket.nodes.node_floor_task_ticket_effect.tests.support import ScriptedStore
from omnimarket.nodes.node_floor_task_ticket_payload_compute.handlers.handler_floor_task_ticket_payload import (
    HandlerFloorTaskTicketPayload,
)
from omnimarket.nodes.node_floor_task_ticket_payload_compute.tests.support import (
    LINE,
    PARENT,
    PLAN,
    PLAN_WITHOUT_TICKET,
)
from tests.nodes.node_floor_task_ticket.support import (
    mint_request_of,
    mint_task,
    payload_request,
    real_gate_create_ticket,
)

pytestmark = pytest.mark.unit

STAMP_PREFIX = "created-by-lane:"


# ------------------------------------------------------------------ criterion 1: the real gate accepts the payload


def test_mint_payload_passes_gate() -> None:
    world = real_gate_create_ticket()
    verdict, minted = mint_task(world.store, LINE, "T1", PLAN)
    assert verdict.verdict == "PAYLOAD"
    assert minted is not None
    assert minted.state == "ticket", minted
    assert minted.ticket == "OMN-91001"
    assert minted.reused is False
    [call] = world.client.calls
    assert call["parent"] == PARENT
    description = str(call["description"])
    assert description.splitlines()[0].startswith(STAMP_PREFIX)
    payload = verdict.payload
    assert payload is not None
    # the stamp is the one line the node adds; the rest is the payload as built, verbatim
    assert description.split("\n", 2)[2] == payload.description
    assert (
        "- [ ] AC1: The node returns the verdict for the fixture -- falsifier: `-k fixture_verdicts`."
        in description
    )
    assert (
        "- [ ] AC2: The same input twice is byte-identical -- falsifier: run twice, compare `model_dump_json`."
        in description
    )
    assert "## Definition of done" in description
    assert f"Floor key: floor:{LINE}:T1" in description.splitlines()
    assert call["label_names"] == ["lane-created"]
    assert world.issues["OMN-91001"]["project"] is None


def test_mint_payload_passes_gate_on_a_stub_gate_with_one_create_carrying_the_parts() -> (
    None
):
    """The hosted run's reading of criterion 1: a stub gate that checks the parts the real gate reads.

    It is not the real gate (the real gate is the test above); it holds the
    payload to the same parts: a parent, a binding line, labelled criteria that each name a falsifier and the Floor key.
    """
    seen: list[ModelFloorTicketCreateRequest] = []

    def gate(request: ModelFloorTicketCreateRequest) -> str | None:
        seen.append(request)
        lines = request.description.splitlines()
        criteria = [line for line in lines if line.startswith("- ")]
        if not request.parent:
            return "missing_parent"
        if not any(line == f"Gate: {request.parent} AC-1" for line in lines):
            return "missing_gate_line"
        if not criteria or any("falsifier:" not in line for line in criteria):
            return "unfalsified_criterion"
        if f"Floor key: {request.floor_key}" not in lines:
            return "missing_floor_key"
        return None

    store = ScriptedStore(InMemoryFloorTicketStore(gate=gate))
    verdict, minted = mint_task(store, LINE, "T1", PLAN)
    assert minted is not None
    assert minted.state == "ticket", minted
    assert store.creates == 1
    [request] = seen
    assert request.parent == PARENT
    assert (
        "- [ ] AC1: The node returns the verdict for the fixture -- falsifier: `-k fixture_verdicts`."
        in request.description
    )
    assert "## Definition of done" in request.description
    assert f"Floor key: floor:{LINE}:T1" in request.description.splitlines()
    # the control: the same gate refuses a payload whose criteria lost their falsifiers
    assert verdict.payload is not None
    stripped = verdict.payload.description.replace("falsifier:", "checked by:")
    assert (
        gate(request.model_copy(update={"description": stripped}))
        == "unfalsified_criterion"
    )


def test_mint_payload_passes_gate_exactly_one_create_and_none_on_the_second_pass() -> (
    None
):
    world = real_gate_create_ticket()
    mint_task(world.store, LINE, "T1", PLAN)
    _, again = mint_task(world.store, LINE, "T1", PLAN)
    assert again is not None
    assert (again.ticket, again.reused) == ("OMN-91001", True)
    assert len(world.client.calls) == 1


def test_mint_gate_refusal_by_the_real_gate_makes_no_ticket() -> None:
    world = real_gate_create_ticket()
    verdict = HandlerFloorTaskTicketPayload().handle(payload_request(LINE, "T1", PLAN))
    good = mint_request_of(verdict)
    # the control: a payload the compute would never build, with the falsifiers taken out of the Definition of done
    bad = good.model_copy(
        update={"description": good.description.replace("falsifier:", "checked by:")}
    )
    refused = HandlerFloorTaskTicketMint(world.store).handle(bad)
    assert refused.state == "refused"
    assert refused.cause is not None
    assert refused.cause.startswith("ticket-refused:")
    assert "unfalsified_criterion" in refused.cause
    assert world.client.calls == []
    assert world.issues == {}


# ------------------------------------------------------------------ criterion 2: no spec, no parent, a refusing gate


@pytest.mark.parametrize(
    ("task", "parts"),
    [
        ("T2", ("falsifier:2",)),
        ("T3", ("criteria",)),
        ("T4", ("integration-check",)),
        ("T99", ("task-section",)),
    ],
)
def test_mint_needs_spec_makes_zero_store_calls(
    task: str, parts: tuple[str, ...]
) -> None:
    store = ScriptedStore()
    verdict, minted = mint_task(store, LINE, task, PLAN)
    assert verdict.verdict == "NEEDS-SPEC"
    assert verdict.missing == parts
    assert minted is None
    assert store.calls == []


def test_mint_no_parent_makes_zero_store_calls() -> None:
    store = ScriptedStore()
    verdict, minted = mint_task(store, LINE, "T1", PLAN_WITHOUT_TICKET)
    assert verdict.verdict == "NO-PARENT"
    assert minted is None
    assert store.calls == []


def test_mint_plan_unread_makes_zero_store_calls() -> None:
    store = ScriptedStore()
    verdict, minted = mint_task(store, LINE, "T1", None)
    assert verdict.verdict == "PLAN-UNREAD"
    assert minted is None
    assert store.calls == []


def test_mint_gate_refusal_makes_no_ticket_and_names_the_codes() -> None:
    seen: list[ModelFloorTicketCreateRequest] = []

    def refuse(request: ModelFloorTicketCreateRequest) -> str | None:
        seen.append(request)
        return "unfalsified_criterion"

    store = ScriptedStore(InMemoryFloorTicketStore(gate=refuse))
    verdict, minted = mint_task(store, LINE, "T1", PLAN)
    assert verdict.verdict == "PAYLOAD"
    assert minted is not None
    assert (minted.state, minted.cause) == (
        "refused",
        "ticket-refused:unfalsified_criterion",
    )
    assert store.inner.tickets == {}
    assert store.calls == ["search_by_floor_key", "create"]
    [request] = seen
    assert request.parent == PARENT
    assert request.floor_key == f"floor:{LINE}:T1"


def test_mint_the_payload_the_gate_sees_is_the_payload_the_compute_built() -> None:
    seen: list[ModelFloorTicketCreateRequest] = []

    def admit(request: ModelFloorTicketCreateRequest) -> str | None:
        seen.append(request)
        return None

    store = ScriptedStore(InMemoryFloorTicketStore(gate=admit))
    verdict, minted = mint_task(store, LINE, "T1", PLAN)
    assert minted is not None
    assert minted.state == "ticket"
    assert verdict.payload is not None
    [request] = seen
    assert (request.title, request.description, request.parent) == (
        verdict.payload.title,
        verdict.payload.description,
        verdict.payload.parent,
    )
    assert payload_request(LINE, "T1", PLAN).task_id == "T1"
