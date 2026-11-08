# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_floor_task_ticket_effect: search before create, fail closed, retry on the next tick."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from omnibase_spi.contracts.services import (
    ModelFloorTicketCreateRequest,
    ModelFloorTicketCreateResult,
    ModelFloorTicketLookupResult,
)
from omnibase_spi.protocols.lane import ProtocolFloorTicketStore
from pydantic import ValidationError

from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_inmemory_floor_ticket_store import (
    InMemoryFloorTicketStore,
    StoredTicket,
)
from omnimarket.nodes.node_floor_task_ticket_effect.container.container_inmemory import (
    build_inmemory_handlers,
)
from omnimarket.nodes.node_floor_task_ticket_effect.handlers import (
    HandlerFloorTaskTicketEntryCheck,
    HandlerFloorTaskTicketMint,
)
from omnimarket.nodes.node_floor_task_ticket_effect.models import (
    ModelFloorTaskEntryTicketRequest,
    ModelFloorTaskTicketMintRequest,
    ModelFloorTaskTicketMintResult,
)
from omnimarket.nodes.node_floor_task_ticket_effect.tests.support import (
    DEADLINE,
    KEY,
    ScriptedStore,
    mint_request,
    released,
    ticket_text,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(__file__).resolve().parent.parent


def mint(
    store: ScriptedStore, request: ModelFloorTaskTicketMintRequest | None = None
) -> ModelFloorTaskTicketMintResult:
    return HandlerFloorTaskTicketMint(store).handle(request or mint_request())


# ------------------------------------------------------------------ the sequence: search, then create, once


def test_mint_creates_one_ticket_when_the_key_has_none_and_reads_it_back() -> None:
    store = ScriptedStore()
    result = mint(store)
    assert result == ModelFloorTaskTicketMintResult(state="ticket", ticket="OMN-70100")
    assert store.calls == ["search_by_floor_key", "create", "read"]
    assert store.creates == 1


def test_mint_search_before_create_a_usable_match_is_used_and_nothing_is_created() -> (
    None
):
    store = ScriptedStore()
    store.add("OMN-70050", description=ticket_text())
    result = mint(store)
    assert result == ModelFloorTaskTicketMintResult(
        state="ticket", ticket="OMN-70050", reused=True
    )
    assert store.creates == 0
    assert store.calls == ["search_by_floor_key", "read"]


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "Done"},
        {"status": "Canceled"},
        {"status": "Cancelled"},
        {"status": "Duplicate"},
        {"has_children": True},
    ],
    ids=["done", "canceled", "cancelled", "duplicate", "umbrella"],
)
def test_mint_search_before_create_only_unusable_matches_create_one_ticket(
    fields: dict[str, Any],
) -> None:
    store = ScriptedStore()
    store.add("OMN-70050", description=ticket_text(), **fields)
    result = mint(store)
    assert result.state == "ticket"
    assert result.reused is False
    assert result.ticket == "OMN-70101"
    assert store.creates == 1


def test_mint_search_before_create_the_oldest_usable_match_is_used() -> None:
    store = ScriptedStore()
    store.add("OMN-70070", description=ticket_text())
    store.add("OMN-70040", description=ticket_text(), status="Done")
    store.add("OMN-70060", description=ticket_text())
    store.add("OMN-9", description=ticket_text(), has_children=True)
    result = mint(store)
    assert (result.ticket, result.reused) == ("OMN-70060", True)
    assert store.creates == 0


def test_mint_search_before_create_a_longer_key_is_not_a_match() -> None:
    store = ScriptedStore()
    store.add("OMN-70050", description=ticket_text(KEY + "0"))
    result = mint(store)
    assert result.ticket == "OMN-70101"
    assert result.reused is False
    assert store.creates == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"project": "Sprint 2026-11-02 -> 2026-11-08"},
        {"labels": ("delegation",)},
        {"labels": ("Delegation-Pilot",)},
        {"pillar": "delegation"},
    ],
    ids=["project", "delegation-label", "delegation-label-cased", "pillar"],
)
def test_mint_old_fill_visible_refused_is_neither_used_nor_replaced(
    fields: dict[str, Any],
) -> None:
    store = ScriptedStore()
    store.add("OMN-70050", description=ticket_text(), **fields)
    result = mint(store)
    assert result == ModelFloorTaskTicketMintResult(
        state="refused", cause="ticket-refused:old-fill-visible"
    )
    assert result.ticket is None
    assert store.creates == 0


def test_mint_old_fill_visible_only_when_it_is_the_oldest_usable_match() -> None:
    store = ScriptedStore()
    store.add("OMN-70050", description=ticket_text(), project="p", status="Done")
    store.add("OMN-70060", description=ticket_text())
    result = mint(store)
    assert (result.ticket, result.reused) == ("OMN-70060", True)


def test_mint_a_match_that_cannot_be_read_ends_the_mint_for_the_tick() -> None:
    store = ScriptedStore()
    store.add("OMN-70050", description=ticket_text())
    store.raise_on["read"] = RuntimeError("secret-looking message")
    result = mint(store)
    assert result == ModelFloorTaskTicketMintResult(
        state="unavailable", cause="read-failed:RuntimeError"
    )
    assert store.creates == 0
    assert "secret" not in (result.cause or "")


# ------------------------------------------------------------------ the deadline and the next tick


def test_mint_timeout_retries_next_tick_search_timeout_ends_the_mint_with_no_ticket() -> (
    None
):
    store = ScriptedStore()
    with released(store):
        store.hold("search_by_floor_key")
        started = time.monotonic()
        result = mint(store)
        assert time.monotonic() - started < 5
    assert result == ModelFloorTaskTicketMintResult(
        state="unavailable", cause="search-timeout"
    )
    assert store.creates == 0


def test_mint_timeout_retries_next_tick_late_create_is_used_when_the_search_returns_it() -> (
    None
):
    store = ScriptedStore()
    with released(store):
        late = store.hold("create")
        first = mint(store)
        assert first == ModelFloorTaskTicketMintResult(
            state="unavailable", cause="create-timeout"
        )
        late.set()
        deadline = time.monotonic() + 5
        while not store.inner.tickets and time.monotonic() < deadline:
            time.sleep(0.01)
        assert list(store.inner.tickets) == ["OMN-70100"]
        before = store.creates
        second = mint(store)
    assert (second.ticket, second.reused) == ("OMN-70100", True)
    assert store.creates == before == 1


def test_mint_timeout_retries_next_tick_creates_once_when_the_search_returns_nothing() -> (
    None
):
    store = ScriptedStore()
    with released(store):
        store.hold("create")
        first = mint(store)
        assert first.cause == "create-timeout"
        # the late create has not landed, and the search finds nothing: the next tick creates, once
        del store.holds["create"]
        second = mint(store)
    assert second.state == "ticket"
    assert second.reused is False
    assert store.creates == 2
    assert second.ticket is not None


def test_mint_timeout_retries_next_tick_a_lagging_search_after_a_late_create_makes_a_second_ticket() -> (
    None
):
    store = ScriptedStore()
    with released(store):
        late = store.hold("create")
        assert mint(store).cause == "create-timeout"
        late.set()
        deadline = time.monotonic() + 5
        while not store.inner.tickets and time.monotonic() < deadline:
            time.sleep(0.01)
        store.lag_search = True
        second = mint(store)
    assert second.state == "ticket"
    assert second.reused is False
    assert len(store.inner.tickets) == 2


def test_mint_timeout_retries_next_tick_a_read_that_runs_past_the_deadline_ends_the_mint() -> (
    None
):
    store = ScriptedStore()
    store.add("OMN-70050", description=ticket_text())
    with released(store):
        store.hold("read")
        result = mint(store)
    assert result == ModelFloorTaskTicketMintResult(
        state="unavailable", cause="read-timeout"
    )
    assert store.creates == 0


def test_mint_a_store_that_raises_is_unavailable_and_names_only_the_exception_type() -> (
    None
):
    store = ScriptedStore()
    store.raise_on["create"] = RuntimeError("token abc123")
    result = mint(store)
    assert result == ModelFloorTaskTicketMintResult(
        state="unavailable", cause="create-failed:RuntimeError"
    )


def test_mint_a_search_the_store_cannot_answer_ends_the_mint() -> None:
    store = ScriptedStore()
    store.search_failure = "linear-down"
    result = mint(store)
    assert result == ModelFloorTaskTicketMintResult(
        state="unavailable", cause="search-failed:linear-down"
    )
    assert store.creates == 0


# ------------------------------------------------------------------ the read-back after a create


def test_mint_readback_after_create_unreadable_stores_nothing() -> None:
    store = ScriptedStore()
    store.read_after_create = ModelFloorTicketLookupResult(state="failed", cause="x")
    result = mint(store)
    assert result == ModelFloorTaskTicketMintResult(
        state="unavailable", cause="readback-unreadable"
    )
    assert result.ticket is None


def test_mint_readback_after_create_missing_stores_nothing() -> None:
    store = ScriptedStore()
    store.read_after_create = ModelFloorTicketLookupResult(state="missing")
    assert mint(store).cause == "readback-unreadable"


@pytest.mark.parametrize(
    ("facts", "why"),
    [
        ({"status": "Canceled"}, "canceled"),
        ({"status": "Done"}, "done"),
        ({"has_children": True}, "umbrella"),
        ({"project": "p"}, "old-fill-visible"),
        ({"labels": ("delegation",)}, "old-fill-visible"),
        ({"description": "no key line"}, "wrong-key"),
    ],
)
def test_mint_readback_after_create_unusable_stores_nothing(
    facts: dict[str, Any], why: str
) -> None:
    store = ScriptedStore()
    base: dict[str, Any] = {
        "state": "found",
        "ticket": "OMN-70100",
        "status": "Backlog",
        "description": ticket_text(),
    }
    store.read_after_create = ModelFloorTicketLookupResult(**{**base, **facts})
    result = mint(store)
    assert result == ModelFloorTaskTicketMintResult(
        state="unavailable", cause=f"readback-unusable:{why}"
    )
    assert result.ticket is None


def test_mint_readback_after_create_timeout_stores_nothing() -> None:
    store = ScriptedStore()
    with released(store):
        # only the read-back runs a read: there is no match to read first
        store.hold("read")
        result = mint(store)
    assert result == ModelFloorTaskTicketMintResult(
        state="unavailable", cause="readback-timeout"
    )


# ------------------------------------------------------------------ the gate inside create


def test_mint_gate_refusal_makes_no_ticket_and_names_the_codes() -> None:
    def refuse(request: ModelFloorTicketCreateRequest) -> str | None:
        return "unfalsified_criterion,missing_gate_line"

    store = ScriptedStore(InMemoryFloorTicketStore(gate=refuse))
    result = mint(store)
    assert result == ModelFloorTaskTicketMintResult(
        state="refused",
        cause="ticket-refused:unfalsified_criterion,missing_gate_line",
    )
    assert store.inner.tickets == {}
    assert store.calls == ["search_by_floor_key", "create"]


def test_mint_a_create_the_store_cannot_answer_ends_the_mint() -> None:
    class Failing(InMemoryFloorTicketStore):
        def create(
            self, request: ModelFloorTicketCreateRequest
        ) -> ModelFloorTicketCreateResult:
            return ModelFloorTicketCreateResult(state="failed", cause="linear-down")

    result = mint(ScriptedStore(Failing()))
    assert result.cause == "create-failed:linear-down"
    assert result.state == "unavailable"


# ------------------------------------------------------------------ the entry ticket check


def check(
    store: ScriptedStore, ticket: str, line: str = "line-a", task: str = "T1"
) -> Any:
    return HandlerFloorTaskTicketEntryCheck(store).handle(
        ModelFloorTaskEntryTicketRequest(
            line_id=line, task_id=task, ticket=ticket, mint_timeout_seconds=DEADLINE
        )
    )


def test_entry_check_usable_ticket_is_usable() -> None:
    store = ScriptedStore()
    store.add("OMN-70050", description=ticket_text())
    verdict = check(store, "OMN-70050")
    assert (verdict.status, verdict.ticket, verdict.refusal) == (
        "usable",
        "OMN-70050",
        None,
    )


@pytest.mark.parametrize(
    ("fields", "refusal"),
    [
        ({"status": "Done"}, "done"),
        ({"status": "Canceled"}, "canceled"),
        ({"has_children": True}, "umbrella"),
        ({"project": "p"}, "old-fill-visible"),
    ],
)
def test_entry_check_refuses_with_its_refusal(
    fields: dict[str, Any], refusal: str
) -> None:
    store = ScriptedStore()
    store.add("OMN-70050", description=ticket_text(), **fields)
    verdict = check(store, "OMN-70050")
    assert (verdict.status, verdict.refusal) == ("refused", refusal)


def test_entry_check_missing_failed_and_late_reads_are_unreadable_never_usable() -> (
    None
):
    store = ScriptedStore()
    assert check(store, "OMN-1").status == "unreadable"
    assert check(store, "OMN-1").cause == "ticket-missing"
    store.add("OMN-70050", description=ticket_text())
    store.raise_on["read"] = RuntimeError("x")
    assert check(store, "OMN-70050").cause == "read-failed:RuntimeError"
    del store.raise_on["read"]
    with released(store):
        store.hold("read")
        assert check(store, "OMN-70050").cause == "read-timeout"


def test_entry_check_a_ticket_without_the_task_key_line_is_unreadable() -> None:
    store = ScriptedStore()
    store.add("OMN-70050", description=ticket_text("floor:other:T9"))
    verdict = check(store, "OMN-70050")
    assert (verdict.status, verdict.cause) == ("unreadable", "no-floor-key-line")


# ------------------------------------------------------------------ G-NODE: contract, keys, seams, profiles


def test_contract_loads_with_archetype_models_routing_and_descriptor() -> None:
    import importlib

    import yaml

    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_floor_task_ticket_effect"
    assert contract["node_type"] == "effect"
    assert contract["descriptor"]["node_archetype"] == "effect"
    routes = {r["operation"]: r for r in contract["handler_routing"]["handlers"]}
    assert set(routes) == {"floor_task_ticket_mint", "floor_task_ticket_entry_check"}
    for route in routes.values():
        for dotted in (route["input_model"], route["output_model"]):
            module, _, name = dotted.rpartition(".")
            assert getattr(importlib.import_module(module), name)
        module = importlib.import_module(route["handler"]["module"])
        assert getattr(module, route["handler"]["name"])
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    assert metadata["name"] == contract["name"]


def test_contract_declares_the_overlay_key_the_node_reads_and_the_example_supplies_it() -> (
    None
):
    import yaml

    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["deployment_overlay"]["fields"] == [
        "floor_fill.mint_timeout_seconds"
    ]
    example = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    assert set(example["floor_fill"]) == {"mint_timeout_seconds"}


def test_seams_in_spi_each_injected_protocol_resolves_into_omnibase_spi() -> None:
    import importlib

    import yaml

    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    listed = []
    for dep in contract["protocol_dependencies"]:
        module, _, name = dep["protocol"].partition(":")
        resolved = getattr(importlib.import_module(module), name)
        listed.append((dep["name"], resolved.__module__, resolved))
    assert [name for name, _, _ in listed] == ["floor_ticket_store"]
    assert all(module.startswith("omnibase_spi") for _, module, _ in listed)
    assert listed[0][2] is ProtocolFloorTicketStore


def test_the_in_memory_store_and_the_scripted_store_satisfy_the_spi_protocol() -> None:
    assert isinstance(InMemoryFloorTicketStore(), ProtocolFloorTicketStore)
    assert isinstance(ScriptedStore(), ProtocolFloorTicketStore)


def test_in_memory_create_never_overwrites_a_seeded_ticket_of_a_noncontiguous_seed() -> (
    None
):
    seeded = StoredTicket(description="Floor key: floor:other:T9\n")
    store = InMemoryFloorTicketStore(tickets={"OMN-2": seeded})
    created = store.create(
        ModelFloorTicketCreateRequest(
            floor_key=KEY, title="t", description=ticket_text(), parent="OMN-1"
        )
    )
    assert created.ticket not in {None, "OMN-2"}
    assert store.tickets["OMN-2"] is seeded
    assert len(store.tickets) == 2


def test_in_memory_profile_builds_both_handlers_on_one_store() -> None:
    mint_handler, entry_handler = build_inmemory_handlers()
    first = mint_handler.handle(mint_request())
    assert first.state == "ticket"
    again = mint_handler.handle(mint_request())
    assert (again.ticket, again.reused) == (first.ticket, True)
    assert first.ticket is not None
    verdict = entry_handler.handle(
        ModelFloorTaskEntryTicketRequest(
            line_id="line-a",
            task_id="T1",
            ticket=first.ticket,
            mint_timeout_seconds=DEADLINE,
        )
    )
    assert verdict.status == "usable"


def test_config_variation_the_deadline_changes_what_ends_the_mint() -> None:
    for seconds, outcome in ((0.05, "unavailable"), (5.0, "ticket")):
        store = ScriptedStore()
        with released(store):
            gate = store.hold("search_by_floor_key")
            timer_release = __import__("threading").Timer(0.3, gate.set)
            timer_release.start()
            result = mint(store, mint_request(timeout=seconds))
            timer_release.join()
        assert result.state == outcome, seconds


def test_missing_key_refused_naming_the_overlay_key() -> None:
    fields: dict[str, Any] = {
        "floor_key": KEY,
        "title": "t",
        "description": "d",
        "parent": "OMN-1",
    }
    with pytest.raises(ValidationError, match=r"floor_fill\.mint_timeout_seconds"):
        ModelFloorTaskTicketMintRequest(**fields)
    with pytest.raises(ValidationError, match=r"floor_fill\.mint_timeout_seconds"):
        ModelFloorTaskEntryTicketRequest.model_validate(
            {"line_id": "l", "task_id": "t", "ticket": "OMN-1"}
        )


@pytest.mark.parametrize("bad", [0, -1, "soon"])
def test_nonsense_deadlines_are_refused(bad: Any) -> None:
    with pytest.raises(ValidationError):
        mint_request(timeout=bad)


def test_result_shape_refuses_a_ticket_with_a_cause_and_a_refusal_with_a_ticket() -> (
    None
):
    for bad in (
        {"state": "ticket"},
        {"state": "ticket", "ticket": "OMN-1", "cause": "x"},
        {"state": "refused", "ticket": "OMN-1", "cause": "x"},
        {"state": "unavailable"},
        {"state": "unavailable", "cause": "x", "reused": True},
    ):
        with pytest.raises(ValidationError):
            ModelFloorTaskTicketMintResult(**bad)


def test_node_holds_no_ticket_id_and_imports_no_other_node() -> None:
    import re

    for path in NODE_DIR.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text()
        assert re.search(r"OMN-[0-9]+", text) is None, path
        assert "omnimarket.nodes.node_" not in text.replace(
            "omnimarket.nodes.node_floor_task_ticket_effect", ""
        ), path
