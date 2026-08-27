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

Deliberately NOT asserted here: that the chain terminalizes. It does not yet —
the constructed arm is the PROJECTION arm, which hands the typed def-B handler a
raw dict (OMN-16767). That fix is omnibase_infra#2937, merged 2026-08-27 but cut
AFTER v0.38.10, so it is not in any release this repo can pin. The chain-gate row
in ``test_event_chain_gate.py`` stays ``xfail(strict=True)`` on OMN-16767 until a
release carries it, at which point that row XPASSes and forces the marker's
removal. This suite covers the strictly earlier question: can the arm be built.
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
from omnibase_infra.runtime.auto_wiring.handler_wiring import _prepare_handler_wiring
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
    """
    assert (
        physical_grant_schema_for_table("tenant", "delegation_routing_tenant_overlay")
        == "public"
    ), (
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
    assert "delegation_routing_tenant_overlay" in names, names


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
def test_projection_arm_constructs_under_every_topology_profile(
    profile_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All seven profiles must build the arm. Under 0.38.9 all seven raised."""
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
