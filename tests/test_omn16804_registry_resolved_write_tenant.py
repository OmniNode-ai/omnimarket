# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16804: the delegation projection writer resolves tenant identity from the
runtime-populated registry, not from a dict compiled into this source tree.

The defect, stated exactly. ``delegation_events.tenant_id`` was keyed through
``_LEGACY_TENANT_UUID_MAP`` -- three entries, hand-maintained, in
``omnimarket/projection/tenant_isolation.py``. Every other tenant raised
``UnmappedTenantIdentityError`` out of the writer, which subclasses
``ValueError`` and is caught nowhere in ``src/``, so the projection runner
classified it POISON and quarantined the event: no row at all. The live registry
held 39 tenants; three were in the map. One real, active, externally-owned
customer -- deliberately NOT named here, because this repo is PUBLIC and
OMN-17288 removed that customer's slug and registry UUID from this tree --
wrote its first delegation 31 minutes after signup and was in the other 36.

Two of these tests are RED-first in the strict sense: ``test_ac1_*`` below
exercise the REAL resolver, not an injected double. The OMN-16690 post-mortem
found that every projection test injected an adapter with no enforcement, which
is precisely why this failure class stayed invisible until it was found by
reading the live table. A double that always resolves cannot fail this way, so
these drive the shipped code path.

What replaces the map is the mechanism the operator green-lit on 2026-08-29
(verbatim: *"Hold + fix mechanism"*): ``tenant_registry_mirror``, materialized
in ``omnidash_analytics`` by ``node_projection_tenant_registry`` from
``onex.tenant.events`` -- the durable outbox ``onex-api`` writes in the same
transaction that provisions the tenant (OMN-16027). The identity therefore comes
from the authenticated context, carried over the bus, and is never caller-
supplied and never derived from the slug.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
    ModelProjectionTaskDelegatedEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.tenant_isolation import (
    _LEGACY_TENANT_UUID_MAP,
    HOUSE_TENANT_SLUG,
    HOUSE_TENANT_UUID,
    UnmappedTenantIdentityError,
    resolve_tenant_uuid,
)
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
    TENANT_REGISTRY_PROJECTION_NODE,
    TenantRegistryResolutionError,
    resolve_registry_tenant_uuid,
    sync_registry_tenant_uuid,
)

# A tenant slug of the shape the live beta signup flow mints, chosen so it can
# never collide with the three compiled entries.
FRESH_TENANT_SLUG = "beta-fresh-6a0c1d33"
FRESH_TENANT_UUID = UUID("6a0c1d33-1d0e-4a3f-9c2f-3b7d2f9a1c04")


def _mirror_row(slug: str, tenant_uuid: UUID) -> dict[str, object]:
    """One row exactly as ``node_projection_tenant_registry`` materializes it."""
    return {
        "tenant_slug": slug,
        "tenant_uuid": str(tenant_uuid),
        "status": "active",
        "source_event_id": str(uuid4()),
    }


def _delegation_event(correlation_id: str, tenant_slug: str | None):
    return ModelProjectionTaskDelegatedEvent(
        correlation_id=correlation_id,
        tenant_id=tenant_slug,
        task_type="code-review",
        delegated_to="node_delegate_skill_orchestrator",
        model_name="qwen3.8",
    )


# ---------------------------------------------------------------------------
# AC1 -- RED-first: the failure this ticket exists for, driven through the real
# resolver rather than an injected double.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ac1_source_compiled_map_cannot_resolve_a_provisioned_tenant() -> None:
    """The pre-fix resolver refuses a fresh tenant -- the DLQ cause, reproduced.

    This is the RED half. ``resolve_tenant_uuid`` is the shipped function the
    writer used to call on every event; driven with a slug the live registry
    knows and the map does not, it raises. Nothing here is mocked.
    """
    assert FRESH_TENANT_SLUG not in _LEGACY_TENANT_UUID_MAP
    with pytest.raises(UnmappedTenantIdentityError):
        resolve_tenant_uuid(FRESH_TENANT_SLUG)


@pytest.mark.unit
def test_ac1_registry_resolves_the_same_tenant_the_map_refuses() -> None:
    """The GREEN half: the identity the registry recorded is the one used."""
    resolved = resolve_registry_tenant_uuid(
        FRESH_TENANT_SLUG, registry_uuid=FRESH_TENANT_UUID
    )
    assert resolved == FRESH_TENANT_UUID


@pytest.mark.unit
def test_ac1_unknown_slug_names_the_projection_not_the_tenant() -> None:
    """AC5 of OMN-16930, restated on the write path: the abort must be readable.

    A slug neither source knows is a statement about the PROJECTION, not about
    the tenant, and the message has to say so -- ``contains null values`` cost a
    week of diagnosis on the apply path for exactly this reason.
    """
    with pytest.raises(TenantRegistryResolutionError) as excinfo:
        resolve_registry_tenant_uuid("t-never-provisioned", registry_uuid=None)
    message = str(excinfo.value)
    assert TENANT_REGISTRY_MIRROR_TABLE in message
    assert TENANT_REGISTRY_PROJECTION_NODE in message
    assert "NOT CAUGHT UP" in message


# ---------------------------------------------------------------------------
# AC3 -- no tenant identity is ever invented or defaulted.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("blank", [None, "", "   "])
def test_ac3_blank_identity_is_refused_never_defaulted(blank: str | None) -> None:
    with pytest.raises(TenantRegistryResolutionError):
        resolve_registry_tenant_uuid(blank, registry_uuid=None)


@pytest.mark.unit
def test_ac3_registry_disagreement_resolves_to_neither() -> None:
    """Two identifiers for one tenant is a fault, not a preference.

    Picking one silently is how a tenant ends up with rows under two keys, which
    is the split this whole ticket exists to prevent. The resolver refuses.
    """
    other = uuid4()
    with pytest.raises(TenantRegistryResolutionError) as excinfo:
        resolve_registry_tenant_uuid(HOUSE_TENANT_SLUG, registry_uuid=other)
    assert "drift" in str(excinfo.value)


@pytest.mark.unit
def test_ac3_legacy_mapping_is_a_fallback_never_an_override() -> None:
    """A slug in BOTH sources resolves to the registry's value, agreement aside.

    Proven by agreement here (the house tenant's UUID is pinned identically in
    both), with disagreement covered above. Order matters: the registry is the
    live authority and the map is closed history.
    """
    assert (
        resolve_registry_tenant_uuid(HOUSE_TENANT_SLUG, registry_uuid=HOUSE_TENANT_UUID)
        == HOUSE_TENANT_UUID
    )


@pytest.mark.unit
def test_lane_without_the_mirror_still_resolves_the_legacy_three() -> None:
    """The change is monotonic on a lane where OMN-16930 has not applied yet.

    ``sync_registry_tenant_uuid`` against a store with no ``tenant_registry_
    mirror`` returns ``None`` -- a DEPLOYMENT fact, not a tenant fact -- so the
    three slugs that resolved before this ticket still resolve. Nothing that
    worked stops working while the migration is still fenced.
    """
    db = InmemoryDatabaseAdapter()
    assert sync_registry_tenant_uuid(db, HOUSE_TENANT_SLUG) is None
    assert (
        resolve_registry_tenant_uuid(
            HOUSE_TENANT_SLUG,
            registry_uuid=sync_registry_tenant_uuid(db, HOUSE_TENANT_SLUG),
        )
        == HOUSE_TENANT_UUID
    )


# ---------------------------------------------------------------------------
# The chain: registry row -> one delegation -> the projection row for that
# correlation carries the registry's tenant, and nothing is quarantined.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chain_fresh_tenant_delegation_projects_under_the_registry_tenant() -> None:
    """The OMN-16804 / GOAL row-5 probe, on the local runtime.

    Create a fresh tenant (materialize its registry row exactly as
    ``node_projection_tenant_registry`` would), run one delegation through the
    real handler, read the projection row back BY CORRELATION, and assert the
    tenant matches. Before this ticket the same event produced no row at all --
    it raised out of the writer and was quarantined.
    """
    db = InmemoryDatabaseAdapter()
    db.upsert(
        TENANT_REGISTRY_MIRROR_TABLE,
        "tenant_slug",
        _mirror_row(FRESH_TENANT_SLUG, FRESH_TENANT_UUID),
    )

    correlation_id = str(uuid4())
    result = HandlerProjectionDelegation().project(
        _delegation_event(correlation_id, FRESH_TENANT_SLUG),
        db,  # type: ignore[arg-type]
    )

    assert result.rows_upserted == 1
    rows = db.query(TABLE, {"correlation_id": correlation_id})
    assert len(rows) == 1
    assert rows[0]["tenant_id"] == str(FRESH_TENANT_UUID)


@pytest.mark.unit
def test_chain_two_tenants_do_not_collapse_onto_one_identity() -> None:
    """Distinct registry tenants keep distinct projection keys.

    The counterfactual to the old behaviour: under the compiled map both of
    these slugs were unmapped, so neither produced a row. Under the registry
    both produce rows, and they are not the same tenant.
    """
    second_slug = "beta-fresh-9e41b7c2"
    second_uuid = UUID("9e41b7c2-77aa-4c1e-b0d2-5f2a1c9e4406")

    db = InmemoryDatabaseAdapter()
    for slug, tenant_uuid in (
        (FRESH_TENANT_SLUG, FRESH_TENANT_UUID),
        (second_slug, second_uuid),
    ):
        db.upsert(
            TENANT_REGISTRY_MIRROR_TABLE, "tenant_slug", _mirror_row(slug, tenant_uuid)
        )

    handler = HandlerProjectionDelegation()
    first_correlation = str(uuid4())
    second_correlation = str(uuid4())
    handler.project(_delegation_event(first_correlation, FRESH_TENANT_SLUG), db)  # type: ignore[arg-type]
    handler.project(_delegation_event(second_correlation, second_slug), db)  # type: ignore[arg-type]

    first = db.query(TABLE, {"correlation_id": first_correlation})[0]
    second = db.query(TABLE, {"correlation_id": second_correlation})[0]
    assert first["tenant_id"] == str(FRESH_TENANT_UUID)
    assert second["tenant_id"] == str(second_uuid)
    assert first["tenant_id"] != second["tenant_id"]


@pytest.mark.unit
def test_chain_unprovisioned_tenant_writes_no_row_at_all() -> None:
    """Fail-closed is preserved: an unattributable event never becomes a row.

    Quarantine is the correct terminal state for an event nobody can attribute.
    What was wrong before was reaching it for ordinary, fully-provisioned
    customers -- not that it exists.
    """
    db = InmemoryDatabaseAdapter()
    correlation_id = str(uuid4())
    with pytest.raises(TenantRegistryResolutionError):
        HandlerProjectionDelegation().project(
            _delegation_event(correlation_id, "t-never-provisioned"),
            db,  # type: ignore[arg-type]
        )
    assert db.query(TABLE, {"correlation_id": correlation_id}) == []
