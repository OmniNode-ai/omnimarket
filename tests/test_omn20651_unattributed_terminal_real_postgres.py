# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20651: real-Postgres proof that an unattributed delegation terminal
leaves no row in ``delegation_events``, and a declared one lands as its own
tenant.

The mock-DB sibling, ``tests/test_omn20651_unattributed_terminal_refused.py``,
asserts the handler's return value and the in-memory table. This file asserts
the consequence in the table that matters: a writer that refused only AFTER a
partial INSERT, or stamped a value the ``uuid`` column then coerced, would
satisfy the sibling and still leave a row behind here.

The defect it pins: with ``ONEX_TENANT_ID`` empty, or set to the writer lane's
own tenant, a terminal that declared no tenant was stamped with that writer
tenant (or the house tenant) and written. The operator ruling of
2026-10-06T17:27Z made that a defect: no row.

Harness: the migrated, disposable schema and the thin sync asyncpg adapter from
``tests/test_omn16804_registry_resolved_write_tenant_real_postgres.py``, reused
rather than copied so the two proofs cannot drift on the migration set. It
SKIPS (never ERRORs) without ``INTEGRATION_POSTGRES_PASSWORD`` or a reachable
database.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.config.settings import get_settings
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
)
from tests.test_omn16804_registry_resolved_write_tenant_real_postgres import (
    real_pg_adapter,
)
from tests.test_omn20651_unattributed_terminal_refused import (
    _delegate_skill_terminal,
    _task_delegated,
)

__all__ = ["real_pg_adapter"]

#: The writer lane's own configured tenant: empty, and a UUID-shaped value the
#: ``uuid`` column would accept -- so a stamped row cannot fail on type alone
#: and make the refusal assertion pass for the wrong reason.
_WRITER_TENANTS = ["", "5d0b2c3e-7f41-4a9b-8e62-0c1d2e3f4a5b"]

_TENANT_A_SLUG = "omn20651-real-pg-tenant-a"
_TENANT_A_UUID = UUID("3c9e1f20-6b7a-4d58-a1e2-9f8e7d6c5b4a")


def _configure_writer(monkeypatch: pytest.MonkeyPatch, writer_tenant: str) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "onex_tenant_id", writer_tenant, raising=True)
    monkeypatch.setattr(settings, "enforce_tenant_isolation", False, raising=True)


def _project(which: str, db: Any, correlation_id: str, tenant: str | None) -> int:
    handler = HandlerProjectionDelegation()
    if which == "project":
        return handler.project(
            _task_delegated(correlation_id, tenant), db
        ).rows_upserted
    return handler.project_delegate_skill_terminal(
        _delegate_skill_terminal(correlation_id, tenant), db
    ).rows_upserted


@pytest.mark.integration
@pytest.mark.parametrize("which", ["project", "project_delegate_skill_terminal"])
@pytest.mark.parametrize("writer_tenant", _WRITER_TENANTS)
def test_an_unattributed_terminal_leaves_no_row(
    which: str,
    writer_tenant: str,
    real_pg_adapter: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_writer(monkeypatch, writer_tenant)
    correlation_id = str(uuid4())

    rows = _project(which, real_pg_adapter, correlation_id, None)

    assert rows == 0
    assert real_pg_adapter.query(TABLE, {"correlation_id": correlation_id}) == [], (
        f"{which} left a delegation_events row for a terminal that declared no "
        f"tenant (writer tenant {writer_tenant!r}) -- OMN-20651"
    )


@pytest.mark.integration
@pytest.mark.parametrize("which", ["project", "project_delegate_skill_terminal"])
@pytest.mark.parametrize("writer_tenant", _WRITER_TENANTS)
def test_a_declared_tenant_lands_as_its_own_uuid(
    which: str,
    writer_tenant: str,
    real_pg_adapter: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: a writer that refused every terminal would satisfy the
    test above completely."""
    _configure_writer(monkeypatch, writer_tenant)
    real_pg_adapter.upsert(
        TENANT_REGISTRY_MIRROR_TABLE,
        "tenant_slug",
        {
            "tenant_slug": _TENANT_A_SLUG,
            "tenant_uuid": _TENANT_A_UUID,
            "status": "active",
            "source_event_id": str(uuid4()),
        },
    )
    correlation_id = str(uuid4())

    rows = _project(which, real_pg_adapter, correlation_id, _TENANT_A_SLUG)

    stored = real_pg_adapter.query(TABLE, {"correlation_id": correlation_id})
    assert rows == 1
    assert len(stored) == 1
    assert stored[0]["tenant_id"] == _TENANT_A_UUID
