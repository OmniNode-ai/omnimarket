# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26220 -- our own work never takes a tenant BYOK tier it was not given.

A tier proof on h053 found ``onex delegate`` for our own work going first to
``byok-anthropic`` (tier ``tenant_overlay``, claude-sonnet-5-5), getting HTTP
401 ``provider_auth_failed``, and only then climbing to the local tier. The
tenant on that run was the house tenant (820272f9-..., ADR-0027). The row it
resolved was the ``*`` (all task types) sentinel that the BYOK bridge writes
from a ``credential-registered`` event: a registration of a KEY, which says
"this tenant's delegations run on this key". That is the customer contract
(OMN-17372). For the house, a stray registration must not outrank the declared
chain; only an exact ``(house, task_type)`` row, which the house writer
(OMN-19186) writes on purpose, is a binding the house was given.
"""

from __future__ import annotations

import pytest

from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.tenant_isolation import (
    HOUSE_TENANT_SLUG,
    HOUSE_TENANT_UUID,
)
from omnimarket.routing.tenant_overlay_resolver import (
    BYOK_ALL_TASK_TYPES,
    TENANT_OVERLAY_TABLE,
    resolve_tenant_overlay,
)

_HOUSE_FORMS = [HOUSE_TENANT_SLUG, str(HOUSE_TENANT_UUID)]


def _seed(
    db: InmemoryDatabaseAdapter, *, tenant_id: str, task_type: str, backend_id: str
) -> None:
    db.upsert(
        TENANT_OVERLAY_TABLE,
        "tenant_id,task_type",
        {
            "tenant_id": tenant_id,
            "task_type": task_type,
            "backend_id": backend_id,
            "provider": "anthropic",
            "endpoint_url": "https://api.anthropic.com/v1/chat/completions",
            "model_name": "claude-sonnet-5-5",
            "secret_ref": "cred_house_anthropic_deadbeef",
            "timeout_ms": None,
            "max_tokens": None,
        },
    )


@pytest.mark.parametrize("house", _HOUSE_FORMS)
def test_house_work_ignores_the_byok_sentinel_row(house: str) -> None:
    db = InmemoryDatabaseAdapter()
    _seed(
        db, tenant_id=house, task_type=BYOK_ALL_TASK_TYPES, backend_id="byok-anthropic"
    )

    assert resolve_tenant_overlay(db, tenant_id=house, task_type="document") is None


@pytest.mark.parametrize("house", _HOUSE_FORMS)
def test_house_work_still_takes_its_own_exact_row(house: str) -> None:
    db = InmemoryDatabaseAdapter()
    _seed(db, tenant_id=house, task_type="document", backend_id="lab-rung")

    resolved = resolve_tenant_overlay(db, tenant_id=house, task_type="document")

    assert resolved is not None
    assert resolved.backend_id == "lab-rung"


def test_a_customer_still_takes_the_byok_sentinel_row() -> None:
    """Positive control: the customer contract (OMN-17372) is unchanged."""
    db = InmemoryDatabaseAdapter()
    _seed(
        db,
        tenant_id="acme-corp",
        task_type=BYOK_ALL_TASK_TYPES,
        backend_id="byok-anthropic",
    )

    resolved = resolve_tenant_overlay(db, tenant_id="acme-corp", task_type="document")

    assert resolved is not None
    assert resolved.backend_id == "byok-anthropic"
    assert resolved.task_type == "document"
