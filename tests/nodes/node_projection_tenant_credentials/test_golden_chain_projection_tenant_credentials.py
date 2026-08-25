"""Golden-chain proof for node_projection_tenant_credentials (OMN-16316).

Gateway value->ref exchange (credential_publisher.py) -> credential-registered
-> this node's projection -> tenant_inference_credentials row -> (later)
GET renders from the bus-backed snapshot.
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
    """The full golden path with a mock DB: register inserts, revoke updates
    the SAME row (never a delete), both against the real project_event()
    entrypoint the live Kafka consumer calls."""
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
    insert_sql = db.execute.call_args[0][0]
    assert "INSERT INTO tenant_inference_credentials" in insert_sql

    revoked = await runner.project_event(
        TOPIC_REVOKED,
        {"tenant_id": "omninode", "api_key_ref": "cred_omninode_openrouter_golden1"},
        _make_meta(offset=1),
    )
    assert revoked is True
    revoke_sql = db.execute.call_args[0][0]
    # OMN-16324: revoke is an UPSERT (never a bare UPDATE) so an out-of-order
    # revoke that arrives before its register can still persist a tombstone
    # row -- see handler_tenant_credentials_projection.py::_project_revoked.
    assert "INSERT INTO tenant_inference_credentials" in revoke_sql
    assert "ON CONFLICT (api_key_ref) DO UPDATE" in revoke_sql
    assert "DELETE" not in revoke_sql.upper()
