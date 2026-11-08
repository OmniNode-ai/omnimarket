# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19986: the node_projection_local_identity contract and its pure plan handler.

Failure modes each test is written against:

* the pure node handler and the store-neutral write decide differently, so the
  SQLite and Postgres paths could store different rows;
* the contract stops declaring the event, the exposure, its tenant scoping or
  its key.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from omnimarket.models.model_local_identity_fold import plan_local_identity_write
from omnimarket.models.model_local_identity_minted import ModelLocalIdentityMinted
from omnimarket.nodes.node_projection_local_identity.handlers.handler_projection_local_identity import (
    HandlerProjectionLocalIdentity,
)
from omnimarket.nodes.node_projection_local_identity.models.model_local_identity_plan import (
    ModelLocalIdentityPlanRequest,
)

pytestmark = pytest.mark.unit

TENANT_A = UUID("6c24401e-0000-4000-8000-00000000000a")
T0 = datetime(2026, 10, 30, 5, 0, tzinfo=UTC)
NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_local_identity"
)


def _event(tenant: UUID, at: datetime) -> ModelLocalIdentityMinted:
    return ModelLocalIdentityMinted(tenant_id=tenant, minted_at=at)


def test_the_node_handler_and_the_store_plan_decide_the_same() -> None:
    stamp = T0 + timedelta(hours=2)
    stored_cases: list[tuple[dict[str, object], ...]] = [
        (),
        ({"tenant_id": str(TENANT_A), "minted_at": T0.isoformat()},),
        ({"tenant_id": str(TENANT_A), "minted_at": T0 + timedelta(minutes=9)},),
    ]
    for stored in stored_cases:
        for at in (T0 - timedelta(minutes=1), T0, T0 + timedelta(minutes=1)):
            event = _event(TENANT_A, at)
            node = HandlerProjectionLocalIdentity().handle(
                ModelLocalIdentityPlanRequest(
                    event=event, stored_rows=stored, updated_at=stamp
                )
            )
            assert node.row == plan_local_identity_write(event, stored, stamp)


def test_the_contract_declares_the_event_and_the_tenant_scoped_exposure() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.evt.omnimarket.local-identity-minted.v1"
    ]
    exposure = contract["projection_api"]
    assert exposure["topic"] == "onex.snapshot.projection.local-identity.v1"
    assert exposure["table"] == "local_identity"
    assert exposure["key_columns"] == ["tenant_id"]
    assert exposure["tenant_column"] == "tenant_id"
    assert exposure["bus_backed"] is True
    assert {"tenant_id", "minted_at"} <= set(exposure["columns"])
