# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19988: the writer confirms a report's tenant against the registry mirror.

The mode-2 observer reports for the runtime's configured tenant, which the
bundle carries as a slug (``ONEX_TENANT_ID``) and records in
``tenant_registry_mirror`` with the uuid it derives. The writer is the one place
that stores a row, so it is where that tenant is confirmed: it reads the mirror
before the write, stores the row under the uuid the registry recorded, and
refuses a tenant the registry does not know.

Failure modes each test is written against:

* F4 a configured tenant the registry does not know is stored anyway, so any
  value in ``ONEX_TENANT_ID`` writes rows nobody can attribute;
* F4b a slug the registry knows is refused, so a stock bundle shows no rows;
* F4c a slug is stored under the slug, not the registry's uuid, so the dashboard
  (which reads by the uuid) never serves it;
* F4d a uuid the registry does not know is stored because it has the right shape;
* F4e the refusal still writes or publishes something, or does not say which
  tenant the registry lacked.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.projection.tenant_registry_resolution import (
    TenantRegistryResolutionError,
)
from tests.test_omn19988_local_environment_writer import (
    T0,
    TENANT,
    _project,
    _report,
    _writer,
)

pytestmark = pytest.mark.unit

SLUG = "local-7eac03d77ba9"
SLUG_UUID = "00000000-0000-4000-8000-0000000000a1"
STRANGER_UUID = "11111111-1111-4111-8111-111111111111"


def test_a_tenant_the_registry_does_not_know_is_refused_and_nothing_is_written() -> (
    None
):
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    writer, conn, published = _writer(table, registry={})
    with pytest.raises(TenantRegistryResolutionError, match=TENANT):
        _project(writer, _report("redpanda", "running", T0))
    assert table == {}
    assert published == []
    assert conn.statements == []


def test_a_slug_the_registry_does_not_know_is_refused_and_nothing_is_written() -> None:
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    writer, conn, published = _writer(table, registry={"someone-else": SLUG_UUID})
    with pytest.raises(TenantRegistryResolutionError, match=SLUG):
        _project(writer, _report("redpanda", "running", T0, tenant=SLUG))
    assert table == {}
    assert published == []
    assert conn.statements == []


def test_a_slug_the_registry_knows_is_stored_under_the_registry_uuid() -> None:
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    writer, conn, published = _writer(table, registry={SLUG: SLUG_UUID})
    assert _project(writer, _report("redpanda", "running", T0, tenant=SLUG))
    assert list(table) == [(SLUG_UUID, "compose", "redpanda")]
    assert conn.tenant_setting == SLUG_UUID
    assert [p["tenant_id"] for p in published] == [SLUG_UUID]
    assert published[0]["row"]["tenant_id"] == SLUG_UUID


def test_a_known_slug_replayed_changes_no_row() -> None:
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    writer, _conn, published = _writer(table, registry={SLUG: SLUG_UUID})
    report = _report("redpanda", "running", T0, tenant=SLUG)
    _project(writer, report)
    stored = {k: dict(v) for k, v in table.items()}
    published.clear()
    _project(writer, report)
    assert table == stored
    assert len(published) == 1  # republished from the stored row, not rewritten


def test_a_uuid_the_registry_holds_is_stored_as_before() -> None:
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    writer, _conn, _published = _writer(table, registry={"known": STRANGER_UUID})
    assert _project(writer, _report("redpanda", "running", T0, tenant=STRANGER_UUID))
    assert list(table) == [(STRANGER_UUID, "compose", "redpanda")]
