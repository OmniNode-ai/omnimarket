# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The projection arm must be CONSTRUCTIBLE under every checked-in topology (OMN-16794).

What went wrong.

``node_delegation_routing_reducer``'s contract declares ``db_io.db_tables`` for
``tenant.delegation_routing_tenant_overlay`` (OMN-15631). Wiring that contract
resolves the table's LOGICAL schema (``tenant``) to the PHYSICAL grant schema the
topology profiles actually declare the TABLE grant on (``public``), via
``omnibase_infra.topology.physical_schema_mapping``. Under omnibase-infra
**0.38.9** that resolver returned ``tenant``, so the privilege check compared
against a schema no profile grants and raised::

    Projection binding 'tenant_projection' principal 'tenant_projection_writer'
    lacks declared read privileges: SELECT on table
    tenant.delegation_routing_tenant_overlay

under ALL SEVEN checked-in profiles. The arm could not be built at all.

The grants were never missing — the principal has ``[INSERT, SELECT, UPDATE]`` on
schema ``public`` in all seven profiles, identically. The bug was that
``delegation_routing_tenant_overlay`` was absent from
``TENANT_TABLES_PHYSICALLY_IN_PUBLIC_UNTIL_OMN15359``; it was added in
``0ca6735fa`` and first released in **v0.38.10**, while this repo's ``uv.lock``
still pinned 0.38.9. So this was a STALE LOCK, not a grant gap, and a
hand-applied ``GRANT`` would have been doubly wrong.

Why this test and not just the lock bump.

The lock bump alone leaves nothing that goes red if the pin regresses, and the
failure is SILENT in the worst way: an unconstructible arm does not degrade, it
changes which arm the handler is wired to. That is the OMN-16796 defect class —
arm selection as a function of the installed patch release rather than of the
contract. This test pins the outcome, so a future resolution below 0.38.10 fails
here with a named cause instead of resurfacing as a dead chain.

What changed at the 0.38.11 pin (OMN-16815) — read before editing an assertion.

This suite originally asserted that wiring SELECTS the projection arm, because
under 0.38.10 it did: ``db_io.db_tables`` alone chose the arm, which is precisely
the OMN-16767 defect (a typed def-B handler handed a raw ``input_data`` dict, and
the delegation chain died on the first attribute access). omnibase_infra#2937,
first released in **v0.38.11**, clears ``db_tables`` for a handler that declares a
concrete BaseModel input, so the correct arm for this contract is now the TYPED
DEF-B arm. Asserting the projection arm here after that fix would be asserting the
defect, so the selection assertion is INVERTED, not deleted — arm IDENTITY is
still pinned, so a silent regression back to the projection arm (the OMN-16767
signature) fails here by name.

That inversion means wiring no longer reaches ``_resolve_projection_database_target``
for this contract, so it can no longer carry the OMN-16794 constructibility claim.
That claim did not move to a comment: it is asserted DIRECTLY against the resolver
in ``test_projection_binding_resolves_under_every_topology_profile`` below, which
is the exact call that raised under 0.38.9, plus the resolver-level pin in
``test_overlay_table_resolves_to_its_physical_grant_schema``. Both survive a
future arm-selection change, because neither goes through wiring at all.

Terminalization is asserted by the chain-gate row in ``test_event_chain_gate.py``,
which stopped being ``xfail(strict=True)`` at this same pin.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.services.service_handler_resolver import ServiceHandlerResolver
from omnibase_core.services.service_local_handler_ownership_query import (
    ServiceLocalHandlerOwnershipQuery,
)
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _prepare_handler_wiring,
    _resolve_projection_database_target,
)
from omnibase_infra.runtime.auto_wiring.models.model_discovered_contract import (
    ModelDiscoveredContract,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.topology import load_topology_profile
from omnibase_infra.topology.physical_schema_mapping import (
    physical_grant_schema_for_table,
)

pytestmark = pytest.mark.unit

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "omnimarket" / "nodes"

_CONTRACT = _SRC_ROOT / "node_delegation_routing_reducer" / "contract.yaml"

# The OMN-15631 tenant-overlay table whose physical-schema resolution is the
# whole subject of this suite.
_OVERLAY_TABLE = "delegation_routing_tenant_overlay"

# Both arms mint their callback as a closure, so __qualname__ is how the SELECTED
# ARM is identified. Asserting only that some dispatcher_id exists would be
# satisfied by EITHER arm, and which one is the whole question: the projection arm
# is the wrong-arm outcome OMN-16767 shipped, and the typed def-B arm is what
# omnibase_infra#2937 (v0.38.11) restored for a handler with a concrete BaseModel
# input.
_PROJECTION_CALLBACK_QUALNAME = "_make_projection_dispatch_callback.<locals>._callback"
_TYPED_DEF_B_CALLBACK_QUALNAME = "_make_dispatch_callback.<locals>._callback"

# Every topology profile checked into omnibase_infra. The original report was
# "unconstructible under ALL SEVEN", so all seven are asserted, not a sample.
_PROFILES = (
    "local",
    "test",
    "onex-dev",
    "stability-test",
    "prod",
    "judge",
    "onex-prod",
)


def _load_contract() -> ModelDiscoveredContract:
    assert _CONTRACT.exists(), f"contract not found at {_CONTRACT}"
    manifest = discover_contracts_from_paths([_CONTRACT])
    assert not manifest.errors, f"the REAL contract failed to parse: {manifest.errors}"
    assert len(manifest.contracts) == 1
    return manifest.contracts[0]


def test_overlay_table_resolves_to_its_physical_grant_schema() -> None:
    """The exact 0.38.9-vs-0.38.10 divergence, pinned as a one-line fact.

    0.38.9 -> 'tenant' (no profile grants there, so the arm cannot build)
    0.38.10+ -> 'public' (matches the TABLE grant all seven profiles declare)

    The schema and table are read off the PARSED contract rather than typed in.
    With hard-coded values this assertion would keep passing after the contract
    moved its logical schema or renamed the table — i.e. it would still be green
    while no longer testing the declared ``db_io`` binding at all.
    """
    contract = _load_contract()
    assert contract.db_io is not None, "contract no longer declares db_io"
    table = next(
        (t for t in contract.db_io.db_tables if t.name == _OVERLAY_TABLE), None
    )
    assert table is not None, (
        f"contract no longer declares the {_OVERLAY_TABLE!r} table; this suite "
        f"is testing a binding that does not exist"
    )

    assert physical_grant_schema_for_table(table.schema, table.name) == "public", (
        "delegation_routing_tenant_overlay is missing from "
        "TENANT_TABLES_PHYSICALLY_IN_PUBLIC_UNTIL_OMN15359 in the INSTALLED "
        "omnibase-infra. That set entry first shipped in v0.38.10 — the lock has "
        "regressed below it (OMN-16794), and the projection arm is now "
        "unconstructible under every topology profile."
    )


def test_contract_still_declares_the_db_io_block() -> None:
    """Non-vacuity guard: if db_io ever disappears, the test below passes for free."""
    contract = _load_contract()
    assert contract.db_io is not None, (
        "contract no longer declares db_io — this suite would then be asserting "
        "nothing, since the projection arm is only selected when db_tables is "
        "non-empty"
    )
    names = [t.name for t in contract.db_io.db_tables]
    assert _OVERLAY_TABLE in names, names


# The projection binding's DSN env var. `_make_projection_dispatch_callback`
# refuses to build a projection callback without it, which is a LATER check than
# the privilege resolution this suite exists to pin.
#
# This constant is why the suite is hermetic. The first version of this test had
# no `monkeypatch.setenv` and passed locally while failing all seven profiles in
# CI — because a developer shell exports a real `OMNIDASH_ANALYTICS_DB_URL` and
# the CI runner does not. That is precisely the OMN-16796 defect class (behaviour
# as a function of ambient environment rather than of the contract), reproduced
# by accident in the test written to guard against it. The value below is a
# throwaway that is never connected to: construction is what is under test, not
# connectivity.
_DSN_ENV_VAR = "OMNIDASH_ANALYTICS_DB_URL"
_DUMMY_DSN = "postgresql://arm-probe:arm-probe@127.0.0.1:5432/arm_probe"


@pytest.mark.parametrize("profile_name", _PROFILES)
def test_projection_binding_resolves_under_every_topology_profile(
    profile_name: str,
) -> None:
    """The OMN-16794 claim, asserted against the resolver that actually raised.

    ``_resolve_projection_database_target`` is the exact call that produced::

        Projection binding 'tenant_projection' principal 'tenant_projection_writer'
        lacks declared read privileges: SELECT on table
        tenant.delegation_routing_tenant_overlay

    under all seven profiles on omnibase-infra 0.38.9. Since 0.38.11 wiring no
    longer routes this contract through that resolver (omnibase_infra#2937 clears
    ``db_tables`` for a typed def-B handler), the constructibility claim is made
    here directly rather than as a side effect of wiring — otherwise the OMN-16794
    coverage would have silently evaporated with the arm change.
    """
    contract = _load_contract()
    assert contract.db_io is not None

    # No assertion on the return value: the claim under test is that resolution
    # SUCCEEDS. Under 0.38.9 this raised for every profile; a privilege or
    # physical-schema regression makes it raise again, by name, here.
    _resolve_projection_database_target(
        tuple(contract.db_io.db_tables),
        load_topology_profile(profile_name),
    )


@pytest.mark.parametrize("profile_name", _PROFILES)
def test_wiring_selects_the_typed_def_b_arm_under_every_topology_profile(
    profile_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All seven profiles must wire, and all seven must pick the TYPED arm.

    Under 0.38.9 all seven raised (unconstructible). Under 0.38.10 all seven
    built the PROJECTION arm, which is the OMN-16767 defect. Under 0.38.11 all
    seven must build the typed def-B arm.
    """
    contract = _load_contract()

    # Pin the DSN rather than inheriting it, so this asserts the same thing on a
    # laptop with a populated env and on a bare CI runner.
    monkeypatch.setenv(_DSN_ENV_VAR, _DUMMY_DSN)

    prepared = _prepare_handler_wiring(
        contract=contract,
        entry=contract.handler_routing.handlers[0],
        dispatch_engine=MessageDispatchEngine(),
        resolver=ServiceHandlerResolver(),
        ownership_query=ServiceLocalHandlerOwnershipQuery(
            local_node_names=frozenset({contract.name})
        ),
        event_bus=EventBusInmemory(environment="arm-probe", group="arm-probe"),
        container=None,
        topology=load_topology_profile(profile_name),
    )

    assert prepared.dispatcher_id, (
        f"[{profile_name}] wiring produced no dispatcher for a contract that "
        f"declares db_io"
    )

    # THE ARM, not just "a dispatcher". Both arms yield a valid dispatcher_id,
    # so which one was selected is the entire assertion. The projection arm is
    # called out by name because that specific value is the OMN-16767 outage
    # signature, not merely "some other arm".
    selected = getattr(prepared.dispatcher, "__qualname__", "")
    assert selected != _PROJECTION_CALLBACK_QUALNAME, (
        f"[{profile_name}] wiring selected the PROJECTION arm for a handler whose "
        f"input is a concrete BaseModel — this is the OMN-16767 regression: the "
        f"handler receives a raw input_data dict and dies on its first attribute "
        f"access. The installed omnibase-infra has regressed below v0.38.11 "
        f"(omnibase_infra#2937)."
    )
    assert selected == _TYPED_DEF_B_CALLBACK_QUALNAME, (
        f"[{profile_name}] expected the TYPED def-B arm "
        f"({_TYPED_DEF_B_CALLBACK_QUALNAME}), got {selected!r}"
    )
