"""Golden-chain proof for node_projection_tenant_credentials (OMN-16316).

Gateway value->ref exchange (credential_publisher.py) -> credential-registered
-> this node's projection -> tenant_inference_credentials row -> (later)
GET renders from the bus-backed snapshot.

OMN-17372 widened the chain rather than redirecting it: the SAME consume now
derives TWO read models from the one event stream -- the ref catalog
(``tenant_inference_credentials``, which the customer sees and whose ref shape
the effect boundary matches to refuse the house-key fallback, OMN-16944) AND
the route that actually selects that key
(``delegation_routing_tenant_overlay``). Neither replaces the other, so this
round trip asserts BOTH halves, in order, on every leg.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection import (
    HandlerTenantCredentialsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.routing.byok_provider_backends import resolve_byok_provider_backend
from omnimarket.routing.tenant_overlay_resolver import BYOK_ALL_TASK_TYPES

_CONTRACT = (
    Path(__file__).parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_tenant_credentials"
    / "contract.yaml"
)
TOPIC_REGISTERED = "onex.evt.omnimarket.credential-registered.v1"
TOPIC_REVOKED = "onex.evt.omnimarket.credential-revoked.v1"
TERMINAL_EVENT = "onex.evt.omnimarket.tenant-credential-projection-applied.v1"


def _make_meta(partition: int = 0, offset: int = 0) -> MessageMeta:
    return MessageMeta(partition=partition, offset=offset, fallback_id="golden-1")


@pytest.mark.unit
def test_contract_declares_the_credential_topics_and_terminal_event() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))

    subscriptions = contract["event_bus"]["subscribe_topics"]
    assert TOPIC_REGISTERED in subscriptions
    assert TOPIC_REVOKED in subscriptions
    # Golden-chain terminal-event pin (state-coverage-gate, OMN-16316): a
    # genuine, non-vacuous comparison against the literal contract declares,
    # not a self-tautology.
    assert contract["terminal_event"] == TERMINAL_EVENT
    assert contract["projection_api"]["table"] == "tenant_inference_credentials"
    assert contract["projection_api"]["bus_backed"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registered_then_revoked_round_trip_projects_correctly() -> None:
    """The full golden path with a mock DB, across BOTH read models.

    Register inserts the catalog row and mints the route; revoke updates the
    SAME catalog row and un-points the SAME route (never a delete, on either
    table) -- all against the real project_event() entrypoint the live Kafka
    consumer calls.

    Asserted over the whole ``call_args_list`` rather than ``call_args``. The
    previous revision read ``call_args`` (the LAST call) as a stand-in for
    "the only call", which silently encoded the pre-OMN-17372 assumption that
    one event produces one write. It does not: OMN-17372 derives a second read
    model from the same consume, so reading only the last call would now prove
    the overlay half while asserting nothing about the catalog half -- exactly
    the half whose refs the effect boundary matches (OMN-16944/OMN-16984).
    Both statements are pinned here, in order.
    """
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=[
            {
                "api_key_ref": "cred_omninode_openrouter_golden1",
                "tenant_id": "omninode",
                "name": "golden-key",
                "provider": "openrouter",
                "created_at": "2026-08-20T00:00:00Z",
                "revoked_at": None,
            }
        ]
    )

    runner = HandlerTenantCredentialsProjectionRunner()
    runner._db = db

    registered = await runner.project_event(
        TOPIC_REGISTERED,
        {
            "tenant_id": "omninode",
            "provider": "openrouter",
            "name": "golden-key",
            "api_key_ref": "cred_omninode_openrouter_golden1",
        },
        _make_meta(offset=0),
    )
    assert registered is True

    # One consume, two writes -- catalog FIRST, then the route. The order is
    # load-bearing, not incidental: _project_routing_overlay's
    # "WHERE NOT EXISTS (... revoked_at IS NOT NULL)" guard reads the catalog
    # row the first statement just produced, so an out-of-order revoke's
    # OMN-16324 tombstone is already visible and blocks the route.
    register_calls = db.execute.call_args_list
    assert len(register_calls) == 2, (
        "credential-registered must derive BOTH read models from the one "
        f"event; saw {len(register_calls)} statement(s)"
    )

    catalog_sql = register_calls[0][0][0]
    assert "INSERT INTO tenant_inference_credentials" in catalog_sql
    assert "ON CONFLICT (api_key_ref) DO UPDATE" in catalog_sql
    assert "DELETE" not in catalog_sql.upper()
    # The ref the effect boundary matches (OMN-16944) is the one catalogued.
    assert register_calls[0][0][1] == "cred_omninode_openrouter_golden1"

    overlay_sql = register_calls[1][0][0]
    assert "INSERT INTO delegation_routing_tenant_overlay" in overlay_sql
    assert "ON CONFLICT (tenant_id, task_type) DO UPDATE" in overlay_sql
    assert "DELETE" not in overlay_sql.upper()
    overlay_args = register_calls[1][0][1:]
    backend = resolve_byok_provider_backend("openrouter")
    assert backend is not None, (
        "openrouter must stay declared in byok_provider_backends.v1.yaml -- an "
        "undeclared provider is catalogued and left unrouted (OMN-17372 "
        "ruling 3), which would make this golden path vacuous"
    )
    # One tenant-scoped sentinel row, on the DECLARED BYOK backend, threading
    # the tenant's OWN ref as secret_ref. Never a platform backend, whose
    # secret_ref is a house credential.
    assert overlay_args[0] == "omninode"
    assert overlay_args[1] == BYOK_ALL_TASK_TYPES
    assert overlay_args[2] == backend.backend_id
    assert overlay_args[5] == "cred_omninode_openrouter_golden1"

    db.execute.reset_mock()

    revoked = await runner.project_event(
        TOPIC_REVOKED,
        {"tenant_id": "omninode", "api_key_ref": "cred_omninode_openrouter_golden1"},
        _make_meta(offset=1),
    )
    assert revoked is True

    revoke_calls = db.execute.call_args_list
    assert len(revoke_calls) == 2, (
        "credential-revoked must reach BOTH read models; saw "
        f"{len(revoke_calls)} statement(s)"
    )

    # OMN-16324: revoke is an UPSERT (never a bare UPDATE) so an out-of-order
    # revoke that arrives before its register can still persist a tombstone
    # row -- see handler_tenant_credentials_projection.py::_project_revoked.
    revoke_sql = revoke_calls[0][0][0]
    assert "INSERT INTO tenant_inference_credentials" in revoke_sql
    assert "ON CONFLICT (api_key_ref) DO UPDATE" in revoke_sql
    assert "DELETE" not in revoke_sql.upper()

    # OMN-17372: the route is un-pointed, not dropped. Dropping the row would
    # make resolve_tenant_overlay miss and fall the tenant through to the
    # platform default ladder -- back onto a HOUSE credential, the outcome
    # ruling 3 forbids. NULLing secret_ref keeps them on their own backend
    # with no key, which fails at the effect boundary instead.
    revoke_overlay_sql = revoke_calls[1][0][0]
    assert "UPDATE delegation_routing_tenant_overlay" in revoke_overlay_sql
    assert "secret_ref = NULL" in revoke_overlay_sql
    assert "DELETE" not in revoke_overlay_sql.upper()
    # Scoped by tenant AND ref, so revoking one credential cannot blank
    # another tenant's route or a newer ref of this tenant's.
    assert revoke_calls[1][0][1:] == ("omninode", "cred_omninode_openrouter_golden1")
