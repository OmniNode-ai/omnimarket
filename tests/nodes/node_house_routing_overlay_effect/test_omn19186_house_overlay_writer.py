# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19186 — the house routing-overlay writer.

AC3 is "a writer exists for house rows that is NOT the BYOK credential
bridge, takes a typed declaration and refuses a partial one". Its falsifier is
a declaration missing a required field that writes a row, so most of this
module is refusals.

AC4 is "a declared-but-unreachable backend is a health fact, not a load-time
refusal". Its falsifier is a write that probes the endpoint and refuses
because nothing answers, so the unreachable-endpoint case is asserted
positively here rather than left to the lab.
"""

from __future__ import annotations

import socket
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_house_routing_overlay_effect.handlers.handler_house_routing_overlay_write import (
    _ENV_OVERLAY_STORE_DSN,
    HandlerHouseRoutingOverlayWrite,
    HouseOverlayWriteError,
)
from omnimarket.nodes.node_house_routing_overlay_effect.models.model_house_routing_overlay import (
    EnumHouseOverlayOperation,
    ModelHouseRoutingOverlayCommand,
    ModelHouseRoutingOverlayDeclaration,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from omnimarket.routing.tenant_overlay_resolver import TENANT_OVERLAY_TABLE

_TIERS = frozenset({"local", "cheap_cloud", "cheap_frontier", "claude"})


class SpyStore:
    """Records writes; performs none. Also records that nothing opened a socket."""

    def __init__(self) -> None:
        self.upserts: list[dict[str, object]] = []
        self.deletes: list[dict[str, object]] = []
        self.delete_result = 1

    def upsert_returning(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
        *,
        tenant: str | None = None,
        returning: tuple[str, ...] = (),
    ) -> list[dict[str, object]]:
        self.upserts.append(
            {
                "table": table,
                "conflict_key": conflict_key,
                "row": dict(row),
                "tenant": tenant,
            }
        )
        return [{key: row.get(key) for key in returning}]

    def delete(
        self,
        table: str,
        filters: Mapping[str, object],
        *,
        tenant: str | None = None,
    ) -> int:
        self.deletes.append(
            {"table": table, "filters": dict(filters), "tenant": tenant}
        )
        return self.delete_result


def _declaration(**overrides: object) -> ModelHouseRoutingOverlayDeclaration:
    fields: dict[str, object] = {
        "backend_id": "lab-omnipc2",
        "endpoint_url": "http://lab-host.invalid:8000/v1/chat/completions",
        "model_name": "Qwen3.8-27B-MTP-IQ4_XS-GGUF",
        "provider": "local",
        "tier_name": "local",
        "task_types": ("code_generation", "refactor"),
    }
    fields.update(overrides)
    return ModelHouseRoutingOverlayDeclaration(**fields)  # type: ignore[arg-type]


def _handler(store: SpyStore) -> HandlerHouseRoutingOverlayWrite:
    return HandlerHouseRoutingOverlayWrite(store, tier_names=_TIERS)


# --- AC3: a partial declaration is un-representable ----------------------------


@pytest.mark.parametrize(
    "missing",
    ["backend_id", "endpoint_url", "model_name", "provider", "tier_name", "task_types"],
)
def test_a_declaration_missing_any_required_field_is_refused(missing: str) -> None:
    fields = {
        "backend_id": "lab-omnipc2",
        "endpoint_url": "http://lab-host.invalid:8000/v1/chat/completions",
        "model_name": "Qwen3.8-27B",
        "provider": "local",
        "tier_name": "local",
        "task_types": ("code_generation",),
    }
    del fields[missing]
    with pytest.raises(ValidationError):
        ModelHouseRoutingOverlayDeclaration(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_required_field_is_refused(blank: str) -> None:
    with pytest.raises(ValidationError):
        _declaration(backend_id=blank)


def test_an_empty_task_type_set_is_refused() -> None:
    with pytest.raises(ValidationError):
        _declaration(task_types=())


def test_the_byok_wildcard_task_type_is_refused() -> None:
    """A house wildcard would take every task class off the ladder in one write."""
    with pytest.raises(ValidationError):
        _declaration(task_types=("*",))


def test_a_repeated_task_type_is_refused() -> None:
    with pytest.raises(ValidationError):
        _declaration(task_types=("code_generation", "code_generation"))


def test_an_unknown_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        _declaration(tenant_id="acme-corp")


@pytest.mark.parametrize(
    "endpoint",
    ["lab-host:8000", "ftp://lab-host:8000", "http://", "   "],
)
def test_a_malformed_endpoint_is_refused(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        _declaration(endpoint_url=endpoint)


def test_a_blank_secret_ref_is_refused_but_an_absent_one_is_not() -> None:
    with pytest.raises(ValidationError):
        _declaration(secret_ref="  ")
    assert _declaration(secret_ref=None).secret_ref is None


@pytest.mark.asyncio
async def test_a_declaration_naming_an_undeclared_tier_is_refused() -> None:
    store = SpyStore()
    command = ModelHouseRoutingOverlayCommand(
        operation=EnumHouseOverlayOperation.DECLARE,
        declaration=_declaration(tier_name="lab_gpus"),
    )
    with pytest.raises(HouseOverlayWriteError, match="not declared by the routing"):
        await _handler(store).handle(command)
    assert store.upserts == [], "a refused declaration must write nothing"


@pytest.mark.asyncio
async def test_an_unreadable_tiers_contract_refuses_rather_than_skipping() -> None:
    store = SpyStore()
    handler = HandlerHouseRoutingOverlayWrite(
        store, tiers_path=Path("/nonexistent/routing_tiers.yaml")
    )
    command = ModelHouseRoutingOverlayCommand(
        operation=EnumHouseOverlayOperation.DECLARE, declaration=_declaration()
    )
    with pytest.raises(HouseOverlayWriteError, match="could not be read"):
        await handler.handle(command)
    assert store.upserts == []


@pytest.mark.asyncio
async def test_a_write_with_no_store_refuses_rather_than_reporting_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deterministic against ambient env: with the DSN this handler's own
    # deployed wiring would resolve against absent, an explicit store=None
    # must still resolve to no store, not a hidden default.
    monkeypatch.delenv(_ENV_OVERLAY_STORE_DSN, raising=False)
    command = ModelHouseRoutingOverlayCommand(
        operation=EnumHouseOverlayOperation.DECLARE, declaration=_declaration()
    )
    with pytest.raises(HouseOverlayWriteError, match="no overlay store"):
        await HandlerHouseRoutingOverlayWrite(None, tier_names=_TIERS).handle(command)


def test_the_deployed_no_arg_wiring_resolves_its_own_store_from_the_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMN-19186 re-proof (prove-101 MSG 2026-09-25T15:12:00Z, ledger:4297).

    The runtime auto-wires this handler on its command topic with NO
    arguments -- ``HandlerHouseRoutingOverlayWrite()`` -- so a store injected
    only by hand-written tests never reaches the deployed path at all. This
    asserts the handler resolves its own write-capable store from the same
    ``OMNIDASH_ANALYTICS_DB_URL`` DSN the read side
    (``resolve_tenant_overlay_db``) already reads, exactly as the deployed
    no-arg construction would.
    """
    monkeypatch.setenv(_ENV_OVERLAY_STORE_DSN, "postgresql://user:pass@host/db")
    handler = HandlerHouseRoutingOverlayWrite()
    assert isinstance(handler._store, PostgresSyncProjectionAdapter)


def test_the_deployed_no_arg_wiring_with_no_dsn_still_has_no_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterpart of the above: no DSN configured means no store is
    resolved, so ``handle()`` keeps refusing (fail-closed unchanged)."""
    monkeypatch.delenv(_ENV_OVERLAY_STORE_DSN, raising=False)
    handler = HandlerHouseRoutingOverlayWrite()
    assert handler._store is None


# --- AC3: what a complete declaration writes -----------------------------------


@pytest.mark.asyncio
async def test_a_complete_declaration_writes_one_house_row_per_task_type() -> None:
    store = SpyStore()
    command = ModelHouseRoutingOverlayCommand(
        operation=EnumHouseOverlayOperation.DECLARE, declaration=_declaration()
    )

    receipt = await _handler(store).handle(command)

    assert receipt.rows_written == 2
    assert receipt.tenant_id == HOUSE_TENANT_SLUG
    assert receipt.backend_id == "lab-omnipc2"
    assert receipt.tier_name == "local"
    assert receipt.task_types == ("code_generation", "refactor")
    assert receipt.secret_ref_declared is False

    assert [call["table"] for call in store.upserts] == [TENANT_OVERLAY_TABLE] * 2
    written_task_types = [call["row"]["task_type"] for call in store.upserts]  # type: ignore[index]
    assert written_task_types == ["code_generation", "refactor"]
    for call in store.upserts:
        row = call["row"]
        assert row["tenant_id"] == HOUSE_TENANT_SLUG  # type: ignore[index]
        assert call["tenant"] == HOUSE_TENANT_SLUG
        assert row["provider"] == "local"  # type: ignore[index]
        assert row["secret_ref"] is None  # type: ignore[index]


def test_the_writer_has_no_way_to_name_a_customer_tenant() -> None:
    """The isolation control on the write side is structural, not a check.

    There is no tenant field on the declaration or the command, so no caller,
    typo or later refactor can point this node at a customer's binding. That is
    the other half of AC2: the resolver scopes reads by tenant_id, and this
    writer cannot produce a row under any tenant but the house.
    """
    assert "tenant_id" not in ModelHouseRoutingOverlayDeclaration.model_fields
    assert "tenant_id" not in ModelHouseRoutingOverlayCommand.model_fields


# --- AC4: unreachable is a health fact, not a refusal --------------------------


@pytest.mark.asyncio
async def test_an_unreachable_endpoint_is_declarable_and_never_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The falsifier for AC4, executed: nothing may open a socket on this path."""

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "the house-overlay writer opened a socket; a declared-but-"
            "unreachable rung is a health fact, not a write-time refusal"
        )

    monkeypatch.setattr(socket, "create_connection", _explode)
    monkeypatch.setattr(socket.socket, "connect", _explode)

    store = SpyStore()
    command = ModelHouseRoutingOverlayCommand(
        operation=EnumHouseOverlayOperation.DECLARE,
        # .invalid is reserved by RFC 2606 and never resolves.
        declaration=_declaration(
            endpoint_url="http://nothing-answers-here.invalid:9/v1/chat/completions"
        ),
    )

    receipt = await _handler(store).handle(command)

    assert receipt.rows_written == 2
    assert receipt.endpoint_reachability_probed is False
    assert (
        receipt.endpoint_url
        == "http://nothing-answers-here.invalid:9/v1/chat/completions"
    )


# --- Retire --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_retire_removes_only_the_named_backend_rows() -> None:
    store = SpyStore()
    command = ModelHouseRoutingOverlayCommand(
        operation=EnumHouseOverlayOperation.RETIRE,
        retire_backend_id="lab-omnipc2",
        retire_task_types=("code_generation", "refactor"),
    )

    receipt = await _handler(store).handle(command)

    assert receipt.rows_removed == 2
    assert receipt.operation is EnumHouseOverlayOperation.RETIRE
    assert [call["filters"] for call in store.deletes] == [
        {
            "tenant_id": HOUSE_TENANT_SLUG,
            "task_type": "code_generation",
            "backend_id": "lab-omnipc2",
        },
        {
            "tenant_id": HOUSE_TENANT_SLUG,
            "task_type": "refactor",
            "backend_id": "lab-omnipc2",
        },
    ]


@pytest.mark.asyncio
async def test_a_retire_that_matched_nothing_reports_zero_not_success() -> None:
    store = SpyStore()
    store.delete_result = 0
    receipt = await _handler(store).handle(
        ModelHouseRoutingOverlayCommand(
            operation=EnumHouseOverlayOperation.RETIRE,
            retire_backend_id="never-declared",
            retire_task_types=("code_generation",),
        )
    )
    assert receipt.rows_removed == 0


def test_a_retire_must_name_what_it_withdraws() -> None:
    with pytest.raises(ValidationError):
        ModelHouseRoutingOverlayCommand(
            operation=EnumHouseOverlayOperation.RETIRE,
            retire_backend_id="lab-omnipc2",
        )
    with pytest.raises(ValidationError):
        ModelHouseRoutingOverlayCommand(
            operation=EnumHouseOverlayOperation.RETIRE,
            retire_task_types=("code_generation",),
        )


def test_a_declare_must_carry_a_declaration_and_a_retire_must_not() -> None:
    with pytest.raises(ValidationError):
        ModelHouseRoutingOverlayCommand(operation=EnumHouseOverlayOperation.DECLARE)
    with pytest.raises(ValidationError):
        ModelHouseRoutingOverlayCommand(
            operation=EnumHouseOverlayOperation.RETIRE,
            declaration=_declaration(),
            retire_backend_id="lab-omnipc2",
            retire_task_types=("code_generation",),
        )


# --- The tier vocabulary is the DEPLOYED one, not a literal in this module ------


@pytest.mark.asyncio
async def test_the_packaged_routing_tiers_contract_declares_the_local_tier() -> None:
    """Positive control on the fail-closed tier check.

    Without this, every tier-refusal test above would still pass if the reader
    returned an empty set for a real contract -- a check that refuses
    everything looks identical to a check that works.
    """
    store = SpyStore()
    handler = HandlerHouseRoutingOverlayWrite(store)  # resolves the packaged path
    receipt = await handler.handle(
        ModelHouseRoutingOverlayCommand(
            operation=EnumHouseOverlayOperation.DECLARE,
            declaration=_declaration(task_types=("code_generation",)),
        )
    )
    assert receipt.rows_written == 1


# --- Contract declaration parity ----------------------------------------------


def test_the_contract_declares_the_topics_this_node_actually_uses() -> None:
    """The node's declared surface, asserted rather than assumed.

    The command topic is what the runtime dispatches on and the terminal event
    is the single typed return the dispatch boundary publishes. There is
    deliberately no failure topic: every refusal on this path RAISES, and a
    declared topic nothing emits is a topic a consumer could wait on forever.
    """
    import yaml

    contract_path = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_house_routing_overlay_effect"
        / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())

    assert contract["node_type"] == "effect"
    assert contract["descriptor"]["node_archetype"] == "effect"
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.house-routing-overlay-write.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.house-routing-overlay-written.v1"
    ]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.house-routing-overlay-written.v1"
    )
    assert contract["event_bus"]["dlq_topics"] == [
        "onex.dlq.omnimarket.house-routing-overlay-write.v1"
    ]
